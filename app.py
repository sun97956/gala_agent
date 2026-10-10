import os

import gradio as gr
import pandas as pd

from run_local import load_answers, load_questions, run_task, submit

# ============================================================
# Run (with answer cache) & Submit
# ============================================================


def run_and_submit_all(rerun_all: bool, profile: gr.OAuthProfile | None):
    if not profile:
        return "Please Login to Hugging Face.", None
    username = profile.username
    space_id = os.getenv("SPACE_ID") or os.getenv("MY_SPACE_ID", f"{username}/Final_Assignment_Template")

    try:
        questions = load_questions(refresh=True)
    except Exception as e:
        return f"Failed fetching questions: {e}", None

    answers = load_answers()
    todo = [q for q in questions if rerun_all or q["task_id"] not in answers]

    if todo:
        try:
            from agent import GAIAAgent

            agent = GAIAAgent()
        except Exception as e:
            return f"Agent initialization failed: {e}", None
        for item in todo:
            try:
                run_task(agent, item)  # answer is saved to answers.json immediately
            except Exception as e:
                print(f"Task {item['task_id']} failed: {e}")

    answers = load_answers()
    rows = [
        {
            "Task ID": q["task_id"],
            "Question": q["question"][:120],
            "Attachment": q.get("file_name") or "",
            "Answer": answers.get(q["task_id"], {}).get("answer", ""),
        }
        for q in questions
    ]

    try:
        result = submit(username, space_id, questions)
        status = (
            "Submission Successful!\n"
            f"User: {result.get('username')}\n"
            f"Score: {result.get('score', 'N/A')}%\n"
            f"Correct: {result.get('correct_count', '?')}/{result.get('total_attempted', '?')}\n"
            f"{result.get('message', '')}"
        )
    except Exception as e:
        status = f"Submission failed: {e}"
    return status, pd.DataFrame(rows)


# ============================================================
# Gradio UI
# ============================================================

with gr.Blocks() as demo:
    gr.Markdown("# GAIA Agent Evaluation Runner")
    gr.Markdown(
        "Answers are cached in `answers.json`; only questions without a cached answer are run. "
        "For development use `python run_local.py` instead (single-task runs, logs, local scoring)."
    )
    gr.LoginButton()
    rerun = gr.Checkbox(label="Re-run all questions (ignore cache)", value=False)
    run_button = gr.Button("Run Evaluation & Submit All Answers")
    status_output = gr.Textbox(label="Status", lines=6)
    results_table = gr.DataFrame(label="Results")
    run_button.click(fn=run_and_submit_all, inputs=[rerun], outputs=[status_output, results_table])


if __name__ == "__main__":
    print("Starting GAIA Agent...")
    demo.launch(debug=True, share=False)
