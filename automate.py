"""Fill the attached Microsoft Form with concise OpenRouter-assisted answers.

Install Selenium and Chrome Canary before running this script. Set
OPENROUTER_API_KEY in the environment, then run:

	python3 automate.py             # fill and leave the form open
	python3 automate.py --submit    # fill and submit the form
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support import expected_conditions as expected
from selenium.webdriver.support.ui import WebDriverWait


FORM_URL = (
	"https://forms.cloud.microsoft/Pages/ResponsePage.aspx?"
	"id=LqsRFStQLU69aPZ59Um1ospzzpW6mNJKgXACXPVpXedUMzlUTk9GNFVEVDNTRjdIRFBFU0hPSzE5Qi4u"
)
THINK_LOG = Path(__file__).with_name("think.json")
DEFAULT_MODEL = "openai/gpt-4o-mini"
CANARY_BINARY = "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"
QUESTION_LABELS = [
	"Which AI Model do you use the most?",
	"What tasks do you normally use it for?",
	"What challenges do you face when using AI models?",
	"What techniques, if any, do you use to get better results?",
	"Do you think using AI for learning affects cognitive ability?",
	"Do you think using AI for doing repetitive tasks should be allowed in universities?",
	"How accurate do you think AI models' performance is for logical and mathematical problems?",
	"Are you able to solve this question?",
]


def load_local_env(path: Path) -> None:
	if not path.exists():
		return
	for line in path.read_text().splitlines():
		line = line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, value = line.split("=", 1)
		os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_local_env(Path(__file__).with_name(".env"))


def ask_openrouter(
	question: str, options: list[str], model: str, multiple: bool
) -> dict[str, Any]:
	"""Return one selected option and a short, non-chain-of-thought rationale."""
	api_key = os.environ.get("OPENROUTER_API_KEY")
	if not api_key:
		raise RuntimeError("Set OPENROUTER_API_KEY before running the script.")

	prompt = {
		"role": "user",
		"content": (
			"You are an average undergraduate SEECS student at NUST. Answer this "
			"survey question naturally and honestly as that persona. Choose only "
			"from the provided options. Return JSON with integer list key 'selected' "
			"(1-based; choose one or more as appropriate) and string key 'summary' "
			"(under 160 characters). Give only "
			"a brief decision summary, not hidden chain-of-thought.\n\n"
			f"Multiple selections allowed: {'yes' if multiple else 'no'}\n"
			f"Question: {question}\nOptions:\n"
			+ "\n".join(f"{index}. {option}" for index, option in enumerate(options, 1))
		),
	}
	payload = json.dumps(
		{
			"model": model,
			"messages": [
				{
					"role": "system",
					"content": "Follow the requested JSON format exactly.",
				},
				prompt,
			],
			"temperature": 0.7,
			"response_format": {"type": "json_object"},
		}
	).encode()
	request = Request(
		"https://openrouter.ai/api/v1/chat/completions",
		data=payload,
		headers={
			"Authorization": f"Bearer {api_key}",
			"Content-Type": "application/json",
			"HTTP-Referer": "https://forms.cloud.microsoft",
			"X-Title": "Microsoft Forms automatic filler",
		},
		method="POST",
	)
	with urlopen(request, timeout=90) as response:
		result = json.load(response)

	content = result["choices"][0]["message"]["content"]
	decision = json.loads(content)
	selected = decision.get("selected", [decision.get("option")])
	selected = [int(option) for option in selected if option is not None]
	if not multiple and len(selected) != 1:
		raise ValueError("Model must select exactly one option for this question.")
	if not selected or any(not 1 <= option <= len(options) for option in selected):
		raise ValueError(f"Model selected invalid options {selected}; there are {len(options)} options.")
	return {
		"selected": selected,
		"summary": str(decision.get("summary", "Selected based on the persona."))[:160],
		"model_version": str(result.get("model", model)),
	}


def visible_questions(driver: webdriver.Chrome) -> list[dict[str, Any]]:
	"""Extract the rendered Microsoft Forms question groups in page order."""
	questions: list[dict[str, Any]] = []
	previous_signature: tuple[bool, tuple[str, ...]] | None = None
	for group in driver.find_elements(By.CSS_SELECTOR, '[role="group"], [role="radiogroup"]'):
		if not group.is_displayed():
			continue
		controls = group.find_elements(By.CSS_SELECTOR, '[role="radio"], [role="checkbox"]')
		if not controls:
			continue
		labels = [
				control.get_attribute("aria-label")
				or control.get_attribute("value")
				or control.text.strip()
				or f"Option {index}"
				for index, control in enumerate(controls, 1)
			]
		multiple = controls[0].get_attribute("role") == "checkbox"
		signature = (multiple, tuple(labels))
		if signature == previous_signature:
			continue
		previous_signature = signature
		questions.append(
			{
				"element": group,
				"question": QUESTION_LABELS[len(questions)] if len(questions) < len(QUESTION_LABELS) else "Survey question",
				"options": labels,
				"controls": controls,
				"multiple": multiple,
			}
		)
	return questions


def choose_driver(headless: bool) -> webdriver.Chrome:
	options = Options()
	browser_binary = os.environ.get("BROWSER_BINARY", CANARY_BINARY)
	if not os.path.exists(browser_binary):
		raise RuntimeError(
			f"Browser not found at {browser_binary}. Set BROWSER_BINARY in .env to the Canary executable path."
		)
	options.binary_location = browser_binary
	if headless:
		options.add_argument("--headless=new")
	options.add_argument("--window-size=1440,1100")
	options.add_argument("--disable-notifications")
	try:
		driver_path = os.environ.get("CHROMEDRIVER_PATH")
		service = Service(driver_path) if driver_path and os.path.exists(driver_path) else None
		return webdriver.Chrome(service=service, options=options)
	except WebDriverException as error:
		raise RuntimeError("Chrome Canary could not be started. Check BROWSER_BINARY and ChromeDriver.") from error


def fill_form(submit: bool, model: str, headless: bool) -> None:
	driver = choose_driver(headless)
	wait = WebDriverWait(driver, 30)
	log: list[dict[str, Any]] = []
	try:
		driver.get(FORM_URL)
		wait.until(expected.presence_of_element_located((By.CSS_SELECTOR, "body")))
		start_button = wait.until(
			lambda browser: next(
				(
					button
					for button in browser.find_elements(
						By.XPATH,
						"//*[(@role='button' or self::button) and contains(normalize-space(.), 'Start now')]",
					)
					if button.is_displayed()
				),
				False,
			)
		)
		start_button.click()
		wait.until(expected.presence_of_element_located((By.CSS_SELECTOR, '[role="radio"], [role="checkbox"]')))
		questions = visible_questions(driver)
		if not questions:
			raise RuntimeError("No answer controls were found after starting the form.")

		for index, item in enumerate(questions, 1):
			decision = ask_openrouter(item["question"], item["options"], model, item["multiple"])
			for option_number in decision["selected"]:
				item["controls"][option_number - 1].click()
			selected_options = [item["options"][option - 1] for option in decision["selected"]]
			entry = {
				"question": item["question"],
				"selected_option": selected_options[0] if len(selected_options) == 1 else selected_options,
				"summary": decision["summary"],
				"model_name": model.rsplit("/", 1)[-1],
				"model_version": decision["model_version"],
			}
			log.append(entry)
			print(
				f"[{index}/{len(questions)}] {entry['selected_option']} "
				f"({entry['model_name']} {entry['model_version']}) - {entry['summary']}"
			)

		THINK_LOG.write_text(json.dumps(log, indent=2, ensure_ascii=True) + "\n")
		print(f"Saved concise decision summaries to {THINK_LOG}")
		if submit:
			submit_button = wait.until(
				expected.element_to_be_clickable(
					(By.XPATH, "//button[contains(., 'Submit')] | //input[@type='submit']")
				)
			)
			submit_button.click()
			print("Form submitted.")
		else:
			print("Form is filled but not submitted. Use --submit to submit it.")
			input("Press Enter to close the browser...")
	finally:
		driver.quit()


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--submit", action="store_true", help="Submit after filling the form.")
	parser.add_argument("--headless", action="store_true", help="Run Chrome Canary without a visible window.")
	parser.add_argument("--model", default=os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL))
	args = parser.parse_args()
	fill_form(args.submit, args.model, args.headless)


if __name__ == "__main__":
	main()