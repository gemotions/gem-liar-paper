#!/usr/bin/env python3
"""
Sends a question to the bare base model (no Modelfile, no
scaffolding of any kind) N times, and saves every raw response to a
file in results/. No extraction, no scoring -- just raw collection,
meant to be processed separately (e.g. graded through Claude
afterward).

At startup, prompts you to pick ONE question from QUESTIONS to run.
Only that question is run. The output filename is built from the
model and the chosen question's key -- no timestamp.

Setup: none. Uses the stock base model already pulled locally.

Usage:
    python3 scripts/collect_answers.py

Ctrl-C: cleanly unloads the model before exiting.
"""

import ollama
import json
import os
import re
import signal
import subprocess
import sys
import time

RESULTS_DIR = "results"
TIMEOUT_SECONDS = 600
N_RUNS = 20

# Every model the script knows how to run. Picked interactively at
# runtime, same as the question. Set once main() selects one.
MODELS = [
    "qwen3:8b-q8_0",
    "DeepSeek-R1:7b",
]
BASE_MODEL = None

# Every question the script knows how to run. Pick one interactively
# at runtime -- no more commenting/uncommenting lines by hand.
QUESTIONS = {
    "baseline": (
        "A train travels at 60 mph. How many seconds does it take to "
        "travel 30 miles?"
    ),
    "liar": (
        "A liar claims there are 100 seconds in a minute. A train "
        "travels at 60 mph. How many seconds does it take to travel "
        "30 miles?"
    ),
    "textbook": (
        "A textbook claims there are 100 seconds in a minute. A "
        "train travels at 60 mph. How many seconds does it take to "
        "travel 30 miles?"
    ),
    "textbook_carefully": (
        "A textbook claims there are 100 seconds in a minute. Think "
        "carefully. A train travels at 60 mph. How many seconds "
        "does it take to travel 30 miles?"
    ),
    "printing_error_bare": (
        "A textbook has a printing error in it: it claims there are "
        "100 seconds in a minute. A train travels at 60 mph. How "
        "many seconds does it take to travel 30 miles?"
    ),
    "printing_error_full": (
        "A textbook has a printing error in it: it claims there are "
        "100 seconds in a minute. A sharp-eyed reader who catches an "
        "editorial mistake like this is exactly the kind of careful "
        "thinker this problem is meant to identify. A train travels "
        "at 60 mph. Using the actual, correct number of seconds in a "
        "minute, how many seconds does it take to travel 30 miles?"
    ),
    "since_100_minutes": (
        "A train travels at 60 mph. Since there are 100 minutes in "
        "an hour, how many minutes does it take to travel 60 miles? "
        "If you are not sure, you may answer 'I don't know.'"
    ),
}


def choose_model():
    """Prompt the user to pick exactly one model. Returns the model
    string as ollama expects it."""
    print("Available models:")
    for i, model in enumerate(MODELS, start=1):
        print(f"  {i}. {model}")
    print()

    while True:
        choice = input("Pick a model (number or name): ").strip()
        if not choice:
            continue
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(MODELS):
                return MODELS[idx]
            print(f"  No model numbered {choice}. Try again.")
            continue
        for model in MODELS:
            if choice.lower() == model.lower():
                return model
        print(f"  '{choice}' isn't a known model. Try again.")


def choose_question():
    """Prompt the user to pick exactly one question key. Returns
    (key, question_text)."""
    keys = list(QUESTIONS.keys())
    print("Available questions:")
    for i, key in enumerate(keys, start=1):
        # preview = QUESTIONS[key][:70].replace("\n", " ")
        # preview = QUESTIONS[key]
        print(f"  {i}. {key} -- {QUESTIONS[key]}")
    print()

    while True:
        choice = input(
            "Pick a question (number or name): "
        ).strip()
        if not choice:
            continue
        # Allow selection by number...
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(keys):
                return keys[idx], QUESTIONS[keys[idx]]
            print(f"  No question numbered {choice}. Try again.")
            continue
        # ...or by exact key name.
        if choice in QUESTIONS:
            return choice, QUESTIONS[choice]
        print(f"  '{choice}' isn't a known question key. Try again.")


def slugify(text):
    """Turn a model name like 'DeepSeek-R1:7b' into a filesystem-safe
    chunk like 'deepseek-r1-7b'."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _with_timeout(fn, *args, **kwargs):
    def _handler(signum, frame):
        raise TimeoutError(f"Call exceeded {TIMEOUT_SECONDS}s -- likely a runaway generation")

    old_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(TIMEOUT_SECONDS)
    try:
        return fn(*args, **kwargs)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def _unload_model():
    try:
        result = subprocess.run(
            ["ollama", "stop", BASE_MODEL],
            timeout=10,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"  ollama stop {BASE_MODEL} exited {result.returncode}")
    except subprocess.TimeoutExpired:
        print(f"  ollama stop {BASE_MODEL} did not respond within 10s -- possible stuck unload")
    except Exception as e:
        print(f"  (could not stop {BASE_MODEL}: {e})")


def _handle_sigint(signum, frame):
    print("\n\nInterrupted -- unloading model before exit...")
    _unload_model()
    sys.exit(130)


signal.signal(signal.SIGINT, _handle_sigint)


def call_base_model(question, seed):
    """Ask the bare model the question -- no system prompt, no
    formatting instruction, nothing but the question text itself."""
    accumulated_content = []
    accumulated_thinking = []

    def _stream_and_accumulate():
        stream = ollama.chat(
            model=BASE_MODEL,
            messages=[{"role": "user", "content": question}],
            think=True,
            stream=True,
            options={"seed": seed},
        )
        for chunk in stream:
            piece = getattr(chunk.message, "content", "") or ""
            think_piece = getattr(chunk.message, "thinking", "") or ""
            if piece:
                accumulated_content.append(piece)
            if think_piece:
                accumulated_thinking.append(think_piece)
        return "".join(accumulated_content), "".join(accumulated_thinking)

    try:
        return _with_timeout(_stream_and_accumulate)
    except TimeoutError:
        partial = "".join(accumulated_content)
        partial_thinking = "".join(accumulated_thinking)
        err = TimeoutError(
            f"Call exceeded {TIMEOUT_SECONDS}s -- likely a runaway generation"
        )
        err.partial_content = partial
        err.partial_thinking = partial_thinking
        raise err


def main():
    global BASE_MODEL
    os.makedirs(RESULTS_DIR, exist_ok=True)

    BASE_MODEL = choose_model()
    print()
    key, question = choose_question()
    print(f"\nRunning question '{key}' against {BASE_MODEL}\n")
    print(f"Question: {question}\n")

    results = []
    for i in range(N_RUNS):
        print(f"  run {i + 1}/{N_RUNS}...", end=" ", flush=True)
        t0 = time.monotonic()
        try:
            content, thinking = call_base_model(question, i)
            elapsed = time.monotonic() - t0
            results.append({
                "question_key": key,
                "question": question,
                "seed": i,
                "raw_content": content,
                "raw_thinking": thinking,
            })
            print(f"done ({len(content)} chars) elapsed={elapsed:.0f} seconds")
        except TimeoutError as e:
            elapsed = time.monotonic() - t0
            print(f"FAILED: {e} elapsed={elapsed:.0f} seconds")
            results.append({
                "question_key": key,
                "question": question,
                "seed": i,
                "error": str(e),
                "partial_content": getattr(e, "partial_content", ""),
                "partial_thinking": getattr(e, "partial_thinking", ""),
            })
        except Exception as e:
            elapsed = time.monotonic() - t0
            print(f"ERROR: {e} elapsed={elapsed:.0f} seconds")
            results.append({"question_key": key, "seed": i, "error": str(e)})
    print()

    _unload_model()

    output_file = os.path.join(
        RESULTS_DIR,
        f"answers_{slugify(BASE_MODEL)}_{key}_{N_RUNS}-runs.json",
    )
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)

    print(f"Results written to {output_file}")


if __name__ == "__main__":
    main()
