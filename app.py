from openai import AsyncOpenAI
from typing import Dict, Any, List
from chainlit.element import Element
import chainlit as cl
import os

import pytesseract
from pdf2image import convert_from_path
import tempfile
import json
from PIL import Image


api_key = os.environ.get("OPENAI_API_KEY")
client = AsyncOpenAI(api_key=api_key)
assistant_id = os.environ.get("ASSISTANT_ID")

# List of allowed mime types
allowed_mime = ["application/pdf"]

ASSISTANT_NAME = "Hematology Assistant"

# Check if the files uploaded are allowed
async def check_files(files: List[Element]):
    for file in files:
        if file.mime not in allowed_mime:
            return False
    return True

async def process_files(files: List[Element]):
    # Conduct OCR on files
    files_ok = await check_files(files)
    file_texts = {}

    if files_ok:
        for file in files:
            with tempfile.TemporaryDirectory() as path:
                images_from_path = convert_from_path(file.path, output_folder=path)
            pdf_string = ""
            for img in images_from_path:
                pdf_string += str(((pytesseract.image_to_string(img)))) + " [NEW PAGE] " 
            file_texts[file.name] = pdf_string
    else:
        file_error_msg = f"Hey, it seems you have uploaded one or more files that we do not support currently, please upload only : {(',').join(allowed_mime)}"
        await cl.Message(content=file_error_msg, author="You").send()

    return file_texts


async def process_thread_message(
    message_references: Dict[str, cl.Message], thread_message
):
    for idx, content_message in enumerate(thread_message.content):
        id = thread_message.id + str(idx)
        if id in message_references:
            msg = message_references[id]
            msg.content = content_message
            await msg.update()
        else:
            obj = json.loads(content_message.text.value)
            message_content = obj.get("message")
            suggestions = obj.get("suggestions") # TODO: test if is not None
            actions = [
                cl.Action(name="run_suggestion", value="run", label=s, description=s)
                for i, s in enumerate(suggestions)
            ]
            message_references[id] = cl.Message(
                author=ASSISTANT_NAME, content=message_content, actions=actions
            )
            await message_references[id].send()


@cl.action_callback("run_suggestion")
async def on_action(action: cl.Action):
    message = await cl.Message(content=action.label, author="You").send()
    await main(message)


@cl.step
def tool():
    return "Response from the tool!"



@cl.on_chat_start
async def start_chat():
    thread = await client.beta.threads.create()
    cl.user_session.set("thread", thread)


@cl.step(name=ASSISTANT_NAME, type="run", root=True)
async def run(thread_id: str, human_query: str, file_texts: dict):
    # Add the message to the thread
    if len(file_texts) != 0:
        human_query += "\n PDF Given: "
        for i, (filename, text) in enumerate(file_texts.items()):
            human_query += f"\n---{filename}---\n" + text + f"\n---END {filename}---\n"

    init_message = await client.beta.threads.messages.create(
        thread_id=thread_id, role="user", content=human_query
    )

    # Create the run
    run = await client.beta.threads.runs.create_and_poll(
        thread_id=thread_id, assistant_id=assistant_id
    )

    message_references = {}  # type: Dict[str, cl.Message]
    step_references = {}  # type: Dict[str, cl.Step]
    tool_outputs = []
    # Periodically check for updates
    while True:
        run = await client.beta.threads.runs.retrieve(
            thread_id=thread_id, run_id=run.id
        )

        # Fetch the run steps
        run_steps = await client.beta.threads.runs.steps.list(
            thread_id=thread_id, run_id=run.id, order="asc"
        )

        for step in run_steps.data:
            # Fetch step details
            run_step = await client.beta.threads.runs.steps.retrieve(
                thread_id=thread_id, run_id=run.id, step_id=step.id
            )
            step_details = run_step.step_details
            # Update step content in the Chainlit UI
            if step_details.type == "message_creation":
                thread_message = await client.beta.threads.messages.retrieve(
                    message_id=step_details.message_creation.message_id,
                    thread_id=thread_id,
                )
                await process_thread_message(message_references, thread_message)

            if (
                run.status == "requires_action"
                and run.required_action.type == "submit_tool_outputs"
            ):
                await client.beta.threads.runs.submit_tool_outputs(
                    thread_id=thread_id,
                    run_id=run.id,
                    tool_outputs=tool_outputs,
                )

        await cl.sleep(2)  # Refresh every 2 seconds
        if run.status in ["cancelled", "failed", "completed", "expired"]:
            break

@cl.on_message  # this function will be called every time a user inputs a message in the UI
async def main(message: cl.Message):
    """
    This function is called every time a user inputs a message in the UI.
    It sends back an intermediate response from the tool, followed by the final answer.

    Args:
        message: The user's message.

    Returns:
        None.
    """

    thread = cl.user_session.get("thread")  # type: Thread
    files_texts = await process_files(message.elements)

    # Call the tool
    # tool()

    # Send the final answer.
    await run(
        thread_id=thread.id, human_query=message.content, file_texts=files_texts
    )