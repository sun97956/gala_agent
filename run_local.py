# run_local.py
"""
Local dev loop for the GAIA final assignment.

  python run_local.py --list                 # status of the 20 questions
  python run_local.py --task 8e867cd7        # run one task (id prefix ok, can repeat --task)
  python run_local.py                        # run every task that has no cached answer
  python run_local.py --force                # re-run everything
  python run_local.py --score                # local score vs GAIA validation answers (needs HF_TOKEN + dataset access)
  python run_local.py --submit --username YOUR_HF_NAME --space-id YOUR_NAME/YOUR_SPACE

Answers are cached in answers.json, full logs per task in logs/<task_id>.log.
"""

import argparse
import json
import os
import re
import string
import sys
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
API_URL = "https://agents-course-unit4-scoring.hf.space"
QUESTIONS_FILE = BASE_DIR / "questions.json"
ANSWERS_FILE = BASE_DIR / "answers.json"
LOG_DIR = BASE_DIR / "logs"


# ------------------------------------------------------------
# Cache helpers (also used by app.py)
# ------------------------------------------------------------

def load_questions(refresh=False):
    if QUESTIONS_FILE.exists() and not refresh:
        return json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    r = requests.get(f"{API_URL}/questions", timeout=30)
    r.raise_for_status()
    qs = r.json()
    QUESTIONS_FILE.write_text(json.dumps(qs, ensure_ascii=False, indent=2), encoding="utf-8")
    return qs


def load_answers():
    if ANSWERS_FILE.exists():
        return json.loads(ANSWERS_FILE.read_text(encoding="utf-8"))
    return {}


class _FileLock:
    def __init__(self, path):
        self.path = str(path) + ".lock"

    def __enter__(self):
        for _ in range(600):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                time.sleep(0.1)
        raise TimeoutError("answers.json lock")

    def __exit__(self, *a):
        os.close(self.fd)
        os.remove(self.path)


def save_answer(task_id, question, answer, seconds):
    with _FileLock(ANSWERS_FILE):
        _save_answer(task_id, question, answer, seconds)


def _save_answer(task_id, question, answer, seconds):
    answers = load_answers()
    answers[task_id] = {
        "answer": answer,
        "question": question[:200],
        "seconds": round(seconds, 1),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    ANSWERS_FILE.write_text(json.dumps(answers, ensure_ascii=False, indent=2), encoding="utf-8")


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
            except UnicodeEncodeError:
                st.write(s.encode("ascii", "replace").decode())
        return len(s)

    def flush(self):
        for st in self.streams:
            st.flush()

    def isatty(self):
        return False


def run_task(agent, item):
    task_id, question = item["task_id"], item["question"]
    file_name = item.get("file_name") or None
    LOG_DIR.mkdir(exist_ok=True)
    start = time.time()
    with open(LOG_DIR / f"{task_id}.log", "w", encoding="utf-8") as log:
        old = sys.stdout
        sys.stdout = Tee(old, log)
        try:
            answer = agent(question, task_id=task_id, file_name=file_name)
        finally:
            sys.stdout = old
    if not str(answer).strip():
        old = load_answers().get(task_id, {}).get("answer")
        print(f"!!!!!!!! EMPTY ANSWER for {task_id[:8]} (model/network error?). "
              f"Keeping the previous cached answer: {old!r}. Check logs/{task_id}.log")
        return old or ""
    save_answer(task_id, question, answer, time.time() - start)
    return answer


# ------------------------------------------------------------
# Local scoring (GAIA official-style normalisation)
# ------------------------------------------------------------

def _norm_num(x):
    for ch in ["$", "%", ","]:
        x = x.replace(ch, "")
    try:
        return float(x)
    except ValueError:
        return None


def _norm_str(s, remove_punct=True):
    s = re.sub(r"\s", "", s).lower()
    if remove_punct:
        s = s.translate(str.maketrans("", "", string.punctuation))
    return s


def gaia_score(model_answer, truth):
    model_answer = str(model_answer)
    truth = str(truth)
    try:
        t_num = float(truth)
    except ValueError:
        t_num = None
    if t_num is not None:
        m = _norm_num(model_answer)
        return m is not None and abs(m - t_num) < 1e-9
    if any(c in truth for c in ",;"):
        t_items = [x.strip() for x in re.split(r"[,;]", truth)]
        m_items = [x.strip() for x in re.split(r"[,;]", model_answer)]
        if len(t_items) != len(m_items):
            return False
        for t, m in zip(t_items, m_items):
            tn = _norm_num(t)
            if tn is not None:
                if _norm_num(m) != tn:
                    return False
            elif _norm_str(m, False) != _norm_str(t, False):
                return False
        return True
    return _norm_str(model_answer) == _norm_str(truth)


def load_ground_truth():
    """GAIA validation answers, for LOCAL evaluation only (gated dataset: accept terms on HF + HF_TOKEN)."""
    from huggingface_hub import hf_hub_download

    token = os.getenv("HF_TOKEN")
    for fname in ("2023/validation/metadata.jsonl", "2023/validation/metadata.level1.jsonl"):
        try:
            p = hf_hub_download("gaia-benchmark/GAIA", fname, repo_type="dataset", token=token)
            truth = {}
            for line in open(p, encoding="utf-8"):
                d = json.loads(line)
                truth[d["task_id"]] = d["Final answer"]
            return truth
        except Exception as e:
            last = e
    try:
        import pandas as pd

        p = hf_hub_download("gaia-benchmark/GAIA", "2023/validation/metadata.parquet",
                            repo_type="dataset", token=token)
        df = pd.read_parquet(p)
        return dict(zip(df["task_id"], df["Final answer"]))
    except Exception as e:
        raise RuntimeError(f"Could not load GAIA ground truth ({last} / {e}). "
                           "Request access at https://huggingface.co/datasets/gaia-benchmark/GAIA "
                           "and set HF_TOKEN.")


# ------------------------------------------------------------
# Submit
# ------------------------------------------------------------

def submit(username, space_id, questions):
    answers = load_answers()
    payload = [
        {"task_id": q["task_id"], "submitted_answer": answers[q["task_id"]]["answer"]}
        for q in questions if q["task_id"] in answers
    ]
    data = {
        "username": username.strip(),
        "agent_code": f"https://huggingface.co/spaces/{space_id}/tree/main",
        "answers": payload,
    }
    r = requests.post(f"{API_URL}/submit", json=data, timeout=120)
    r.raise_for_status()
    return r.json()


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", action="append", help="task_id or prefix (repeatable)")
    ap.add_argument("--force", action="store_true", help="ignore cached answers")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="re-download questions")
    ap.add_argument("--submit", action="store_true")
    ap.add_argument("--username")
    ap.add_argument("--space-id")
    args = ap.parse_args()

    questions = load_questions(args.refresh)
    answers = load_answers()

    if args.list or args.score:
        truth = {}
        if args.score:
            try:
                truth = load_ground_truth()
            except Exception as e:
                print(e)
                return
        correct = 0
        for q in questions:
            a = answers.get(q["task_id"], {}).get("answer")
            mark = ""
            if args.score:
                ok = a is not None and q["task_id"] in truth and gaia_score(a, truth[q["task_id"]])
                correct += ok
                mark = "OK " if ok else "XX "
            f = " [file]" if q.get("file_name") else ""
            print(f"{mark}{q['task_id'][:8]}{f:7} | {str(a)[:40]:40} | {q['question'][:60]!r}")
        if args.score:
            print(f"\nLocal score: {correct}/{len(questions)} = {100 * correct / len(questions):.0f}%")
        return

    if args.submit:
        if not (args.username and args.space_id):
            print("--submit needs --username and --space-id")
            return
        print(submit(args.username, args.space_id, questions))
        return

    if args.task:
        todo = [q for q in questions if any(q["task_id"].startswith(t) for t in args.task)]
    else:
        todo = [q for q in questions if args.force or q["task_id"] not in answers]
    if not todo:
        print("Nothing to run (all cached). Use --force or --task.")
        return

    from agent import GAIAAgent

    agent = GAIAAgent()
    for i, item in enumerate(todo, 1):
        print(f"\n######## [{i}/{len(todo)}] {item['task_id']}")
        ans = run_task(agent, item)
        print(f"######## ANSWER: {ans!r}")


if __name__ == "__main__":
    main()
