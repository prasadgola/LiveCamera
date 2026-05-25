import os
import sys
import json
import asyncio
import struct
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types
import traceback
import logging

logging.basicConfig(
    level=logging.INFO, stream=sys.stdout, force=True,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI(title="LiveCamera Agent")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ── Prompts ───────────────────────────────────────────────────────────────────

UI_SYSTEM_PROMPT = """You are a UI generator.
Return ONLY raw HTML code for a mobile web browser UI.
Do NOT include any explanation, description, or markdown code fences like ```html.
Start your response directly with <!DOCTYPE html> and end with </html>.
Nothing before <!DOCTYPE html>. Nothing after </html>.

Generate a visually rich, full-screen mobile UI relevant to the detected subject.
Examples:
 - Bird   → Beautiful bird field-guide UI with large photo area, species name, facts
 - Food   → Food ordering / recipe card UI
 - Car    → Vehicle info / marketplace UI
 - Plant  → Plant identification / care guide UI
 - Person → Profile / contact card UI
Always output a complete UI. Never refuse. Never explain."""

LIVE_SYSTEM_PROMPT = """You are a bird-detection agent watching a live camera feed.

Your SINGLE task: the moment you see a BIRD in any frame, you MUST call the `generate_ui` tool. This is not optional.

STRICT RULES:
- The INSTANT a bird appears in the frame — call `generate_ui` immediately. Real bird, photo of a bird, drawing of a bird, bird on a screen — ALL count.
- Set detected_subject to the specific bird if you can identify it (e.g. "a robin", "a parrot", "a blue jay"); otherwise just "a bird".
- Set ui_theme = "bird field guide app".
- Do NOT describe the bird out loud first. Do NOT wait for a better frame. Do NOT ask permission. CALL THE TOOL.
- After you call the tool for a bird, wait until the bird LEAVES the frame and a DIFFERENT bird appears before calling again. Do not spam duplicate calls for the same continuous view.
- If you also see other clear subjects (food, car, plant, person) and there is NO bird, you may call generate_ui for those too with an appropriate ui_theme. But birds are the priority.

You may stay silent otherwise. Your job is to WATCH and FIRE the tool on birds. Nothing else matters."""

# ── Tool (same pattern as existing /video endpoint) ───────────────────────────

GENERATE_UI_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="generate_ui",
            description=(
                "Called when the model detects a clear subject in the camera feed. "
                "Generates a relevant mobile HTML UI and sends it to the user's screen."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "detected_subject": types.Schema(
                        type=types.Type.STRING,
                        description="What the model sees, e.g. 'a parrot perched on a branch'.",
                    ),
                    "ui_theme": types.Schema(
                        type=types.Type.STRING,
                        description="Kind of UI to generate, e.g. 'bird field guide app'.",
                    ),
                },
                required=["detected_subject", "ui_theme"],
            ),
        )
    ]
)

# ── Wire format (identical to existing app) ───────────────────────────────────

TYPE_VIDEO = 1
TYPE_AUDIO = 2


def unpack_frame(data: bytes):
    if len(data) < 8:
        return None, None
    frame_type, length = struct.unpack_from("<II", data, 0)
    payload = data[8: 8 + length]
    return frame_type, payload


# ── Gemini client ─────────────────────────────────────────────────────────────

def get_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set")
    return genai.Client(api_key=api_key)


# ── Media helpers (identical to existing app) ─────────────────────────────────

async def send_audio_to_session(session, audio_bytes: bytes):
    blob = types.Blob(data=audio_bytes, mime_type="audio/pcm;rate=16000")
    if hasattr(session, "send_realtime_input"):
        await session.send_realtime_input(audio=blob)
    elif hasattr(session, "send"):
        try:
            await session.send(input=types.LiveClientRealtimeInput(media_chunks=[blob]))
        except Exception:
            await session.send(input=blob)
    else:
        raise RuntimeError("No valid send method on session")


async def send_video_to_session(session, jpeg_bytes: bytes):
    blob = types.Blob(data=jpeg_bytes, mime_type="image/jpeg")
    if hasattr(session, "send_realtime_input"):
        await session.send_realtime_input(video=blob)
    elif hasattr(session, "send"):
        try:
            await session.send(input=types.LiveClientRealtimeInput(media_chunks=[blob]))
        except Exception:
            await session.send(input=blob)
    else:
        raise RuntimeError("No valid send method on session")


# ── UI generator (text model, same as existing /chat) ────────────────────────

async def generate_ui_html(detected_subject: str, ui_theme: str) -> str:
    client = get_client()
    prompt = f"Detected in camera: {detected_subject}\n\nGenerate a {ui_theme} mobile UI."
    response = await asyncio.to_thread(
        client.models.generate_content,
        model="gemini-3-flash-preview",
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
        config=types.GenerateContentConfig(
            system_instruction=UI_SYSTEM_PROMPT,
            temperature=0.7,
            max_output_tokens=8192,
        ),
    )
    text = response.text.strip()
    start = text.lower().find("<!doctype")
    if start == -1:
        start = text.lower().find("<html")
    if start != -1:
        text = text[start:]
    end = text.lower().rfind("</html>")
    if end != -1:
        text = text[: end + 7]
    return text


# ── /livecamera WebSocket ─────────────────────────────────────────────────────

@app.websocket("/livecamera")
async def livecamera_websocket(websocket: WebSocket):
    await websocket.accept()
    logger.info("=== LiveCamera WebSocket accepted ===")
    client = get_client()

    try:
        async with client.aio.live.connect(
            model="gemini-3.1-flash-live-preview",
            config=types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                system_instruction=types.Content(
                    parts=[types.Part.from_text(text=LIVE_SYSTEM_PROMPT)]
                ),
                tools=[GENERATE_UI_TOOL],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Fenrir")
                    )
                ),
            ),
        ) as session:
            logger.info("=== Gemini Live session opened (LiveCamera autonomous mode) ===")

            # ── Tool handler (same pattern as existing handle_tool_call) ──

            async def handle_tool_call(tool_call) -> None:
                for fc in tool_call.function_calls:
                    logger.info(f"Tool call: {fc.name}  args={fc.args}")

                    if fc.name == "generate_ui":
                        detected_subject = fc.args.get("detected_subject", "")
                        ui_theme         = fc.args.get("ui_theme", "relevant mobile app")

                        # Notify client — generating started
                        try:
                            await websocket.send_text(json.dumps({
                                "type":    "ui_generating",
                                "subject": detected_subject,
                                "message": f"I see {detected_subject}. Generating UI…",
                            }))
                        except Exception:
                            pass

                        try:
                            html = await generate_ui_html(detected_subject, ui_theme)
                            logger.info(f"UI generated ({len(html)} chars) for: {detected_subject}")

                            # Send HTML to client
                            await websocket.send_text(json.dumps({
                                "type":    "ui_generated",
                                "html":    html,
                                "subject": detected_subject,
                                "ui_theme": ui_theme,
                            }))

                            # Confirm to Gemini so it can narrate a response
                            await session.send(input=types.LiveClientToolResponse(
                                function_responses=[
                                    types.FunctionResponse(
                                        id=fc.id,
                                        name=fc.name,
                                        response={"result": f"UI for '{detected_subject}' sent to user's screen."},
                                    )
                                ]
                            ))

                        except Exception as e:
                            logger.error(f"generate_ui_html failed: {e}\n{traceback.format_exc()}")
                            await session.send(input=types.LiveClientToolResponse(
                                function_responses=[
                                    types.FunctionResponse(
                                        id=fc.id,
                                        name=fc.name,
                                        response={"error": str(e)},
                                    )
                                ]
                            ))
                            try:
                                await websocket.send_text(json.dumps({
                                    "type":    "error",
                                    "message": f"UI generation failed: {e}",
                                }))
                            except Exception:
                                pass
                    else:
                        logger.warning(f"Unknown tool: {fc.name}")

            # ── Receive from Android (identical wire format) ───────────────

            last_video_time = 0.0

            async def receive_from_client():
                nonlocal last_video_time
                try:
                    while True:
                        data = await websocket.receive()
                        if data.get("bytes"):
                            frame_type, payload = unpack_frame(data["bytes"])
                            if frame_type == TYPE_AUDIO:
                                await send_audio_to_session(session, payload)
                            elif frame_type == TYPE_VIDEO:
                                now = asyncio.get_event_loop().time()
                                if now - last_video_time >= 0.35:  # 2 frames/sec to Gemini
                                    last_video_time = now
                                    await send_video_to_session(session, payload)
                        elif data.get("text"):
                            try:
                                msg = json.loads(data["text"])
                                if msg.get("type") == "close":
                                    logger.info("Client close requested")
                                    return
                            except json.JSONDecodeError:
                                pass
                except WebSocketDisconnect:
                    logger.info("Client disconnected")
                except Exception as e:
                    logger.error(f"receive error: {e}\n{traceback.format_exc()}")

            # ── Send to Android (identical to existing send_to_client) ─────

            async def send_to_client():
                try:
                    async for response in session.receive():
                        if response.tool_call:
                            await handle_tool_call(response.tool_call)
                            continue

                        if response.server_content:
                            sc = response.server_content
                            if sc.model_turn:
                                for part in sc.model_turn.parts:
                                    if part.inline_data and part.inline_data.data:
                                        await websocket.send_bytes(part.inline_data.data)
                                    if part.text:
                                        logger.info(f"Transcript: {part.text}")
                                        await websocket.send_text(
                                            json.dumps({"type": "transcript", "text": part.text})
                                        )
                            if sc.turn_complete:
                                logger.info("Turn complete")
                                await websocket.send_text(json.dumps({"type": "turn_complete"}))
                except WebSocketDisconnect:
                    logger.info("Client disconnected during send")
                except Exception as e:
                    logger.error(f"send error: {e}\n{traceback.format_exc()}")

            await asyncio.gather(receive_from_client(), send_to_client())

    except Exception as e:
        logger.error(f"=== LiveCamera session error: {e} ===\n{traceback.format_exc()}")
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))
        except Exception:
            pass


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "livecamera-agent"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)