# agent.py
import os
import re
import time
from pathlib import Path

import json

from smolagents import CodeAgent, OpenAIModel

from prompt import SYSTEM_PROMPT
from tools import (
    ALL_TOOLS,
    AUDIO_EXT,
    IMAGE_EXT,
    ensure_attachment,
    read_file,
    transcribe_audio,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ============================================================
# Config (override with environment variables / .env)
# ============================================================

API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")
TEXT_MODEL = os.getenv("TEXT_MODEL", "deepseek-flash")
MAX_STEPS = int(os.getenv("MAX_STEPS", "15"))

AUTHORIZED_IMPORTS = [
    "pandas", "numpy", "openpyxl", "json", "csv", "re", "math", "statistics", "fractions", "decimal",
    "collections", "itertools", "functools", "datetime", "time", "unicodedata", "string",
    "chess", "bs4", "requests", "io",
]

BAD_ANSWER_PATTERNS = [
    r"unable to determine", r"\bunknown\b", r"cannot be determined", r"could not (find|determine)",
    r"not enough information", r"i (cannot|can't|could not)", r"<code>", r"```",
]


# ============================================================
# Answer cleaning
# ============================================================

def clean_answer(ans):
    """Strip things that break exact-match grading."""
    s = str(ans if ans is not None else "").strip()
    s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()
    s = s.replace("**", "").strip()
    s = re.sub(r"^(final answer|answer)\s*[:：]\s*", "", s, flags=re.I).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'`":
        s = s[1:-1].strip()
    # a single number with thousands separators -> plain digits
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):
        s = s.replace(",", "")
    return s


def answer_check(final_answer, memory, agent=None):
    """smolagents final_answer_check: reject non-answers so the agent keeps working."""
    s = str(final_answer).strip()
    if not s:
        raise AssertionError("Empty answer. Find the information and give a concrete answer.")
    for pat in BAD_ANSWER_PATTERNS:
        if re.search(pat, s, flags=re.I):
            raise AssertionError(
                "Your final answer is a non-answer or contains code. Try another source/approach, "
                "and if still uncertain give your single best-supported concrete answer."
            )
    if len(s) > 300:
        raise AssertionError("Final answer is too long. Return ONLY the short answer, no explanation.")
    return True


# ============================================================
# DeepSeek output fix
# ============================================================
# deepseek-flash sometimes answers in its NATIVE tool-call markup instead of a <code> block:
#   <｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="code"><｜｜DSML｜｜ parameter ...>print(1)</｜｜DSML｜｜ parameter>...
# smolagents can't parse that and wastes a step ("regex pattern <code>(.*?)</code> was not found").
# We rewrite it into the expected <code>...</code> block before smolagents parses it.

_DSML = r"[｜|]{1,2}\s*DSML\s*[｜|]{1,2}"


def fix_dsml(text):
    if not text or "DSML" not in text:
        return text
    m = re.search(rf"<{_DSML}\s*invoke[^>]*>(.*?)</{_DSML}", text, re.S)
    if not m:
        return text
    body = re.sub(rf"^\s*<{_DSML}\s*parameter[^>]*>", "", m.group(1)).strip()
    if body.startswith("{"):  # JSON arguments, e.g. {"code": "..."}
        try:
            args = json.loads(body)
            body = next((v for v in args.values() if isinstance(v, str)), body)
        except Exception:
            pass
    thought = text[: re.search(rf"<{_DSML}", text).start()].strip() or "Thought: running code."
    return f"{thought}\n<code>\n{body}\n</code>"


class DeepSeekModel(OpenAIModel):
    def generate(self, *args, **kwargs):
        msg = super().generate(*args, **kwargs)
        if isinstance(msg.content, str) and "DSML" in msg.content:
            msg.content = fix_dsml(msg.content)
        return msg


# ============================================================
# Agent
# ============================================================

class GAIAAgent:

    def __init__(self, max_steps=MAX_STEPS, verbosity_level=1):
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("Missing DEEPSEEK_API_KEY")

        self.model = DeepSeekModel(
            model_id=TEXT_MODEL,
            api_base=API_BASE,
            api_key=api_key,
            client_kwargs={"timeout": 180, "max_retries": 2},
        )
        self.max_steps = max_steps
        self.verbosity_level = verbosity_level
        print(f"[GAIA] initialized: model={TEXT_MODEL} max_steps={max_steps}")

    def _new_agent(self):
        # fresh agent per task: no state leaking between questions
        return CodeAgent(
            tools=ALL_TOOLS,
            model=self.model,
            instructions=SYSTEM_PROMPT,
            max_steps=self.max_steps,
            additional_authorized_imports=AUTHORIZED_IMPORTS,
            final_answer_checks=[answer_check],
            max_print_outputs_length=20000,
            verbosity_level=self.verbosity_level,
            executor_kwargs={
                # default is 30s per code block, which kills audio/video/chess tools mid-run
                "timeout_seconds": 900,
                "additional_functions": {"repr": repr},
            },
        )

    # --------------------------------------------------------
    # Build task prompt (+ deterministic pre-processing of attachments)
    # --------------------------------------------------------

    def build_task(self, question, task_id=None, file_name=None):
        import tools as _tools

        _tools.CURRENT_QUESTION = question  # lets web_search drop pages that just copy the question
        images = []
        parts = [f"QUESTION:\n{question}"]

        path = ensure_attachment(task_id, file_name) if file_name else None
        if file_name and path is None:
            parts.append(f"\nNOTE: the question has an attachment '{file_name}' but it could not be downloaded.")

        if path is not None:
            ext = path.suffix.lower()
            parts.append(f"\nATTACHED FILE: {path}\n(file type: {ext}; pass this path to the file tools)")

            if ext in AUDIO_EXT:
                print("[GAIA] transcribing audio...")
                parts.append("AUDIO TRANSCRIPT (auto, may contain small errors):\n" + transcribe_audio(str(path)))
            elif ext in IMAGE_EXT:
                try:
                    from PIL import Image

                    with Image.open(path) as im:
                        images.append(im.convert("RGB").copy())
                    parts.append("The image is attached to this message. You can also query it with analyze_image.")
                except Exception as e:
                    parts.append(f"(could not load image for direct viewing: {e})")
            elif ext == ".py":
                parts.append("PYTHON SOURCE:\n" + read_file(str(path))[:15000]
                             + "\n\nUse run_python_file to get the actual output.")
            else:
                parts.append("FILE PREVIEW:\n" + read_file(str(path))[:8000])

        parts.append(
            "\nSolve it step by step with the tools, verify, then call final_answer() "
            "with ONLY the answer in the exact requested format."
        )
        return "\n".join(parts), images

    # --------------------------------------------------------
    # Solve
    # --------------------------------------------------------

    def solve(self, question, task_id=None, file_name=None):
        start = time.time()
        print(f"\n================\nTASK {task_id}\nFILE {file_name or '-'}")

        try:
            task, images = self.build_task(question, task_id, file_name)
        except Exception as e:
            print(f"[GAIA] attachment pre-processing failed: {e}")
            task, images = f"QUESTION:\n{question}", []

        result = None
        try:
            self._last_agent = self._new_agent()
            result = self._last_agent.run(task, images=images or None)
        except Exception as e:
            print(f"[GAIA ERROR] {type(e).__name__}: {e}")
            if images:  # e.g. model rejected the image payload -> retry text-only
                try:
                    self._last_agent = self._new_agent()
                    result = self._last_agent.run(task)
                except Exception as e2:
                    print(f"[GAIA ERROR] retry failed: {type(e2).__name__}: {e2}")

        answer = clean_answer(result)
        if self._looks_bad(answer):
            print(f"[GAIA] raw answer unusable ({answer[:80]!r}...), extracting from the work log")
            answer = clean_answer(self._rescue_answer(question, answer))
        print(f"[GAIA DONE] {time.time() - start:.1f}s -> {answer!r}")
        return answer

    @staticmethod
    def _looks_bad(answer):
        if not answer or len(answer) > 200:
            return True
        return any(re.search(p, answer, flags=re.I) for p in BAD_ANSWER_PATTERNS)

    def _rescue_answer(self, question, raw):
        """When the run ended without a clean final_answer (e.g. max steps), ask the model once more,
        giving it the evidence collected in the last steps."""
        agent = getattr(self, "_last_agent", None)
        evidence = []
        if agent is not None:
            for step in agent.memory.steps[-8:]:
                obs = getattr(step, "observations", None)
                if obs:
                    evidence.append(str(obs)[:3000])
        prompt = (
            "You were solving the question below. Here are the tool outputs you collected.\n"
            "Give ONLY the final answer (a number, a few words, or a comma-separated list), "
            "no explanation, no code. If unsure, give your best guess.\n\n"
            f"QUESTION:\n{question}\n\nTOOL OUTPUTS:\n" + "\n---\n".join(evidence)
            + f"\n\nYOUR LAST (unusable) OUTPUT:\n{str(raw)[:2000]}\n\nFINAL ANSWER:"
        )
        try:
            msg = self.model.generate([{"role": "user", "content": [{"type": "text", "text": prompt}]}])
            text = (msg.content or "").strip()
            return text.splitlines()[-1] if text else raw
        except Exception as e:
            print(f"[GAIA] rescue failed: {e}")
            return raw

    def __call__(self, question, task_id=None, file_name=None):
        return self.solve(question, task_id, file_name)


# backwards-compatible name used by the old app.py
GAIABasicAgent = GAIAAgent
