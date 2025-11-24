import os
import sys
import asyncio
import aiohttp
import httpx
import time
import json
import atexit
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Fallback for Python < 3.9
    try:
        from backports.zoneinfo import ZoneInfo
    except ImportError:
        # Final fallback - use UTC
        ZoneInfo = None

from dotenv import load_dotenv
from system_prompt import SYSTEM_PROMPT

# Load environment variables from .env file
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pyngrok import ngrok

from pipecat.frames.frames import EndFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.google.gemini_live.llm_vertex import GeminiLiveVertexLLMService
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

from loguru import logger

# Configure logger
logger.remove(0)
logger.add(sys.stderr, level="DEBUG")

# Global variable to store the ngrok tunnel
ngrok_tunnel = None

# FastAPI app
app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
DAILY_API_KEY = os.getenv("DAILY_API_KEY", "")
GOOGLE_CLOUD_PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT_ID", "")
GOOGLE_CLOUD_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
GOOGLE_VERTEX_CREDENTIALS = os.getenv("GOOGLE_VERTEX_CREDENTIALS", "")

if not DAILY_API_KEY:
    raise ValueError("DAILY_API_KEY must be set")
if not GOOGLE_CLOUD_PROJECT_ID:
    raise ValueError("GOOGLE_CLOUD_PROJECT_ID must be set")
if not GOOGLE_VERTEX_CREDENTIALS:
    raise ValueError("GOOGLE_VERTEX_CREDENTIALS must be set")


def start_ngrok_tunnel(port=8000):
    """Start ngrok tunnel and return the public URL."""
    global ngrok_tunnel
    
    # Get ngrok auth token from environment or use default
    ngrok_auth_token = os.getenv("NGROK_AUTH_TOKEN")
    
    if ngrok_auth_token:
        # Set the authtoken
        ngrok.set_auth_token(ngrok_auth_token)
        logger.info("Using ngrok auth token from environment")
    else:
        logger.warning("NGROK_AUTH_TOKEN not set in environment, using free ngrok (may have limitations)")
    
    # Start the tunnel
    ngrok_tunnel = ngrok.connect(port, "http")
    
    # Get the public URL
    public_url = ngrok_tunnel.public_url
    
    logger.info("=" * 60)
    logger.info("🚀 ngrok tunnel started successfully!")
    logger.info(f"📞 Public URL: {public_url}")
    logger.info(f"🌐 Access your bot at: {public_url}/start")
    logger.info(f"💚 Health check: {public_url}/health")
    logger.info("=" * 60)
    
    # Register cleanup function
    atexit.register(cleanup_ngrok)
    
    return public_url


def cleanup_ngrok():
    """Clean up ngrok tunnel on exit."""
    global ngrok_tunnel
    if ngrok_tunnel:
        try:
            ngrok.disconnect(ngrok_tunnel.public_url)
            ngrok.kill()
            logger.info("ngrok tunnel closed")
        except Exception as e:
            logger.error(f"Error closing ngrok tunnel: {e}")


def fix_credentials():
    """
    Fix GOOGLE_VERTEX_CREDENTIALS so Pipecat can parse it.
    Supports both file paths and JSON strings (including quoted JSON strings from .env files).
    """
    creds = GOOGLE_VERTEX_CREDENTIALS
    
    if not creds:
        raise ValueError("GOOGLE_VERTEX_CREDENTIALS environment variable is not set")
    
    # Strip whitespace
    creds = creds.strip()
    
    # Remove surrounding quotes if present (handles .env files that quote the JSON string)
    if (creds.startswith('"') and creds.endswith('"')) or (creds.startswith("'") and creds.endswith("'")):
        creds = creds[1:-1]
    
    # Check if it looks like JSON (starts with { or [)
    if creds.startswith('{') or creds.startswith('['):
        # Try to parse as JSON first
        try:
            creds_dict = json.loads(creds)
            # Ensure proper newline formatting for private_key
            if "private_key" in creds_dict:
                creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
            return json.dumps(creds_dict)
        except json.JSONDecodeError:
            # If JSON parsing fails, continue to check if it's a file path
            pass
    
    # Determine the file path - try multiple locations
    file_path = None
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Check if it's an absolute path
    if os.path.isabs(creds):
        if os.path.isfile(creds):
            file_path = creds
    # Check if it exists as a relative path from current working directory
    elif os.path.isfile(creds):
        file_path = os.path.abspath(creds)
    # Check if it exists relative to the script directory
    else:
        potential_path = os.path.join(script_dir, creds)
        if os.path.isfile(potential_path):
            file_path = potential_path
    
    # If it ends with .json but we haven't found it yet, assume it's a file path
    # and try relative to script directory
    if not file_path and creds.endswith('.json'):
        potential_path = os.path.join(script_dir, creds)
        if os.path.isfile(potential_path):
            file_path = potential_path
    
    # If we found a file path, read from it
    if file_path and os.path.isfile(file_path):
        try:
            with open(file_path, 'r') as f:
                creds_dict = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            raise ValueError(f"Failed to read credentials from file '{file_path}': {e}") from e
    else:
        # Last attempt: try to parse as JSON (might be unquoted JSON string)
        try:
            creds_dict = json.loads(creds)
        except json.JSONDecodeError as e:
            raise ValueError(f"GOOGLE_VERTEX_CREDENTIALS is not valid JSON and not a valid file path. Value: '{creds[:50]}...' Error: {e}") from e
    
    # Ensure proper newline formatting for private_key
    if "private_key" in creds_dict:
        creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

    return json.dumps(creds_dict)


def get_current_datetime_info():
    """
    Get current date and time information in Asia/Kolkata timezone.
    Returns formatted strings for use in system prompt.
    """
    try:
        # Get current time in Asia/Kolkata timezone
        if ZoneInfo is not None:
            tz = ZoneInfo("Asia/Kolkata")
            now = datetime.now(tz)
            timezone_name = "Asia/Kolkata (IST)"
        else:
            # Fallback to UTC + 5:30 offset manually
            from datetime import timezone
            ist_offset = timedelta(hours=5, minutes=30)
            tz = timezone(ist_offset)
            now = datetime.now(tz)
            timezone_name = "IST (UTC+5:30)"
        
        # Format date as YYYY-MM-DD
        current_date = now.strftime("%Y-%m-%d")
        
        # Format time as HH:MM (24-hour format)
        current_time = now.strftime("%H:%M")
        
        # Get day of week
        day_of_week = now.strftime("%A")
        
        # Get readable date format
        readable_date = now.strftime("%B %d, %Y")  # e.g., "December 31, 2025"
        
        # Calculate tomorrow's date (timedelta is already imported at top of file)
        tomorrow = now + timedelta(days=1)
        tomorrow_date = tomorrow.strftime("%Y-%m-%d")
        tomorrow_readable = tomorrow.strftime("%B %d, %Y")
        tomorrow_day = tomorrow.strftime("%A")
        
        return {
            "current_date": current_date,
            "current_time": current_time,
            "day_of_week": day_of_week,
            "readable_date": readable_date,
            "tomorrow_date": tomorrow_date,
            "tomorrow_readable": tomorrow_readable,
            "tomorrow_day": tomorrow_day,
            "timezone": timezone_name
        }
    except Exception as e:
        logger.error(f"Error getting current datetime: {e}")
        # Fallback to UTC if timezone fails
        now = datetime.now()
        return {
            "current_date": now.strftime("%Y-%m-%d"),
            "current_time": now.strftime("%H:%M"),
            "day_of_week": now.strftime("%A"),
            "readable_date": now.strftime("%B %d, %Y"),
            "tomorrow_date": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
            "tomorrow_readable": (now + timedelta(days=1)).strftime("%B %d, %Y"),
            "tomorrow_day": (now + timedelta(days=1)).strftime("%A"),
            "timezone": "UTC (fallback)"
        }


async def fetch_detailed_information(params: FunctionCallParams):
    """Fetch detailed information from the preprocessor API for queries outside the system prompt"""
    try:
        query = params.arguments["query"]
        phone_number = params.arguments["phone_number"]
        
        # Extract exactly 10 digits from the phone number
        # Remove all non-digit characters first
        digits_only = ''.join(filter(str.isdigit, phone_number))
        
        # If the number starts with "91" and has more than 10 digits, remove the "91" prefix
        # to get the actual 10-digit phone number
        if digits_only.startswith("91") and len(digits_only) > 10:
            # Remove the "91" prefix to get the 10-digit number
            phone_number_clean = digits_only[2:]  # Remove first 2 digits (91)
        elif len(digits_only) > 10:
            # If it's longer than 10 digits but doesn't start with 91, take last 10 digits
            phone_number_clean = digits_only[-10:]
        elif len(digits_only) < 10:
            # If less than 10 digits, pad with zeros (shouldn't happen if agent confirmed)
            phone_number_clean = digits_only.zfill(10)
        else:
            # Exactly 10 digits
            phone_number_clean = digits_only
        
        # Ensure we have exactly 10 digits
        if len(phone_number_clean) != 10:
            raise ValueError(f"Phone number must be exactly 10 digits, got {len(phone_number_clean)} digits")
        
        # Add "91" prefix before sending to API
        phone_number_with_prefix = f"91{phone_number_clean}"
        
        logger.info(f"Calling preprocessor API with query: {query}, number: {phone_number_with_prefix} (10 digits: {phone_number_clean})")
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://preprocessor-739298578243.us-central1.run.app/query",
                json={
                    "query": query,
                    "number": phone_number_with_prefix
                }
            )
            response.raise_for_status()
            data = response.json()
            
            # Return the summary from the API response
            if data.get("status") == "success":
                summary = data.get("summary", "")
                logger.info(f"Preprocessor API returned success. Summary length: {len(summary)}")
                await params.result_callback({
                    "summary": summary,
                    "whatsapp_sent": data.get("whatsapp_status", {}).get("status") == "success",
                    "email_sent": True  # Assuming email is sent by the preprocessor
                })
            else:
                logger.warning(f"Preprocessor API returned non-success status: {data.get('status')}")
                await params.result_callback({
                    "summary": "I apologize, but I couldn't fetch the detailed information at this moment. Please try again.",
                    "whatsapp_sent": False,
                    "email_sent": False
                })
    except Exception as e:
        logger.error(f"Error fetching detailed information: {e}", exc_info=True)
        await params.result_callback({
            "summary": "I apologize, but I'm having trouble accessing the detailed information right now. Let me help you with general information instead.",
            "whatsapp_sent": False,
            "email_sent": False
        })


async def book_appointment(params: FunctionCallParams):
    """Book an appointment with the Disha team by making a calendar event"""
    try:
        title = params.arguments.get("title", "Introductory Meeting with Disha Communications")
        date = params.arguments["date"]
        start_time = params.arguments["start_time"]
        end_time = params.arguments["end_time"]
        description = params.arguments.get("description", "General introductory meeting to discuss Disha Communications' services and how we can help your business.")
        location = params.arguments.get("location", "online")
        
        logger.info(f"Booking appointment: {title} on {date} from {start_time} to {end_time}")
        
        # Make POST request to calendar endpoint with exact format
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://preprocessor-739298578243.us-central1.run.app/calendar",
                json={
                    "title": title,
                    "date": date,
                    "start_time": start_time,
                    "end_time": end_time,
                    "description": description,
                    "location": location
                }
            )
            response.raise_for_status()
            data = response.json()
            
            # Check if there was a conflict
            if data.get("status") == "conflict":
                conflicts = data.get("conflicts", [])
                conflict_message = f"I'm sorry, but there are {len(conflicts)} overlapping appointment(s) at that time. "
                if conflicts:
                    conflict_list = ", ".join([c.get("title", "Untitled") for c in conflicts])
                    conflict_message += f"The conflicting events are: {conflict_list}. "
                conflict_message += "Please choose a different time slot."
                
                await params.result_callback({
                    "status": "conflict",
                    "message": conflict_message,
                    "conflicts": conflicts
                })
            elif data.get("status") == "success":
                event = data.get("event", {})
                success_message = f"Great! I've successfully booked your appointment '{title}' on {date} from {start_time} to {end_time}."
                if event.get("htmlLink"):
                    success_message += f" You'll receive a calendar invitation shortly."
                
                await params.result_callback({
                    "status": "success",
                    "message": success_message,
                    "event_id": event.get("id"),
                    "event_link": event.get("htmlLink")
                })
            else:
                await params.result_callback({
                    "status": "error",
                    "message": "I apologize, but I couldn't book the appointment at this moment. Please try again."
                })
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error booking appointment: {e.response.status_code} - {e.response.text}")
        if e.response.status_code == 409:  # Conflict
            try:
                data = e.response.json()
                conflicts = data.get("conflicts", [])
                conflict_message = f"I'm sorry, but there are {len(conflicts)} overlapping appointment(s) at that time. Please choose a different time slot."
                await params.result_callback({
                    "status": "conflict",
                    "message": conflict_message,
                    "conflicts": conflicts
                })
            except:
                await params.result_callback({
                    "status": "conflict",
                    "message": "I'm sorry, but there's a scheduling conflict at that time. Please choose a different time slot."
                })
        else:
            await params.result_callback({
                "status": "error",
                "message": "I apologize, but I couldn't book the appointment at this moment. Please try again."
            })
    except Exception as e:
        logger.error(f"Error booking appointment: {e}", exc_info=True)
        await params.result_callback({
            "status": "error",
            "message": "I apologize, but I'm having trouble booking the appointment right now. Please try again later."
        })


async def create_daily_room() -> tuple[str, str]:
    """Create a Daily room and return the URL and token"""
    async with aiohttp.ClientSession() as session:
        # Create room
        async with session.post(
            "https://api.daily.co/v1/rooms",
            headers={
                "Authorization": f"Bearer {DAILY_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "properties": {
                    "exp": int(time.time()) + 3600,  # 1 hour from now
                    "enable_chat": False,
                    "enable_emoji_reactions": False,
                }
            },
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                logger.error(f"Failed to create room: {response.status} - {error_text}")
                raise Exception(f"Failed to create Daily room: {response.status}")
            
            room_data = await response.json()
            logger.debug(f"Room data: {room_data}")
            
            room_url = room_data.get("url")
            room_name = room_data.get("name")
            
            if not room_url or not room_name:
                logger.error(f"Missing url or name in room response: {room_data}")
                raise Exception("Invalid room data from Daily API")

        # Create token
        async with session.post(
            "https://api.daily.co/v1/meeting-tokens",
            headers={
                "Authorization": f"Bearer {DAILY_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "properties": {
                    "room_name": room_name,
                    "is_owner": True,
                }
            },
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                logger.error(f"Failed to create token: {response.status} - {error_text}")
                raise Exception(f"Failed to create Daily token: {response.status}")
            
            token_data = await response.json()
            logger.debug(f"Token data: {token_data}")
            token = token_data.get("token")
            
            if not token:
                logger.error(f"Missing token in response: {token_data}")
                raise Exception("Invalid token data from Daily API")

    logger.info(f"Successfully created room: {room_url}")
    return room_url, token


async def run_bot(room_url: str, token: str):
    """Run the voice bot in the Daily room"""
    transport = None
    try:
        logger.info(f"Starting bot for room: {room_url}")
        
        # Initialize transport with Silero VAD
        transport = DailyTransport(
            room_url,
            token,
            "Voice Bot",
            DailyParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                video_out_enabled=False,
                vad_analyzer=SileroVADAnalyzer(
                    params=VADParams(
                        stop_secs=0.3,
                        min_volume=0.6,
                    )
                ),
                transcription_enabled=True,
            ),
        )

        # Get project configuration
        project_id = GOOGLE_CLOUD_PROJECT_ID
        location = GOOGLE_CLOUD_LOCATION
        model_id = "gemini-live-2.5-flash-preview-native-audio-09-2025"
        
        # Build the full model path
        model_path = f"projects/{project_id}/locations/{location}/publishers/google/models/{model_id}"
        
        logger.info(f"Using Vertex AI model: {model_path}")

        # Get current date and time information
        datetime_info = get_current_datetime_info()
        logger.info(f"Current date/time context: {datetime_info['current_date']} {datetime_info['current_time']} ({datetime_info['timezone']})")
        
        # Inject current date/time into system prompt
        datetime_context = f"""

## CURRENT DATE AND TIME INFORMATION

**IMPORTANT: Use this information when booking appointments or answering questions about dates/times.**

- **Current Date**: {datetime_info['readable_date']} ({datetime_info['day_of_week']})
- **Current Date (YYYY-MM-DD format)**: {datetime_info['current_date']}
- **Current Time**: {datetime_info['current_time']} ({datetime_info['timezone']})
- **Tomorrow's Date**: {datetime_info['tomorrow_readable']} ({datetime_info['tomorrow_day']})
- **Tomorrow's Date (YYYY-MM-DD format)**: {datetime_info['tomorrow_date']}

**When booking appointments:**
- If user says "today" or "now", use: {datetime_info['current_date']}
- If user says "tomorrow", use: {datetime_info['tomorrow_date']}
- Always use YYYY-MM-DD format for dates in the book_appointment tool
- Always use HH:MM format (24-hour) for times in the book_appointment tool
- Current timezone is {datetime_info['timezone']}

"""
        
        # Combine system prompt with datetime context
        system_instruction = SYSTEM_PROMPT + datetime_context

        # Define the detailed information tool
        detailed_info_function = FunctionSchema(
            name="get_detailed_information",
            description="Fetch detailed information from our knowledge base for queries that are specific, particular, or outside the general system prompt information. Use this tool when the user asks about specific details, technical specifications, or information that requires looking up detailed documentation. This tool will also send a WhatsApp message with PDF and email to the user automatically.",
            properties={
                "query": {
                    "type": "string",
                    "description": "The specific question or query the user wants detailed information about (e.g., 'How many hanging lights are present at the 54?', 'What are the technical specifications of X?')",
                },
                "phone_number": {
                    "type": "string",
                    "description": "The user's phone number (10 digits, will be prefixed with +91 automatically)",
                },
            },
            required=["query", "phone_number"],
        )

        # Define the appointment booking tool
        book_appointment_function = FunctionSchema(
            name="book_appointment",
            description="Book an appointment with the Disha Communications team. Use this tool when the user wants to schedule a meeting, consultation, or demo. You must collect the date, start_time, and end_time from the user. Title and description will default to a general introductory meeting if not provided. Location defaults to 'online'. IMPORTANT: Make the tool call FIRST, then confirm the booking based on the response.",
            properties={
                "date": {
                    "type": "string",
                    "description": "The date of the appointment in YYYY-MM-DD format (e.g., '2025-12-31'). This is REQUIRED.",
                },
                "start_time": {
                    "type": "string",
                    "description": "The start time of the appointment in HH:MM format (24-hour format, e.g., '14:00' for 2:00 PM). This is REQUIRED.",
                },
                "end_time": {
                    "type": "string",
                    "description": "The end time of the appointment in HH:MM format (24-hour format, e.g., '15:00' for 3:00 PM). This is REQUIRED.",
                },
                "title": {
                    "type": "string",
                    "description": "Optional title for the meeting. Defaults to 'Introductory Meeting with Disha Communications' if not provided. Only provide if user specifies a specific title.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional description of the appointment. Defaults to a general introductory meeting description if not provided. Only provide if user specifies what to discuss.",
                },
                "location": {
                    "type": "string",
                    "description": "Location for the meeting. Defaults to 'online'. Only provide if user specifies a different location.",
                },
            },
            required=["date", "start_time", "end_time"],
        )

        tools = ToolsSchema(standard_tools=[detailed_info_function, book_appointment_function])

        # Initialize Vertex AI LLM Service with tools
        llm = GeminiLiveVertexLLMService(
            credentials=fix_credentials(),
            project_id=project_id,
            location=location,
            model=model_path,
            system_instruction=system_instruction,
            voice_id="Aoede",  # Options: Aoede, Charon, Fenrir, Kore, Puck
            tools=tools,
        )

        # Register the functions
        llm.register_function("get_detailed_information", fetch_detailed_information)
        llm.register_function("book_appointment", book_appointment)

        # Create context with initial greeting and user information collection
        context = LLMContext(
            [
                {
                    "role": "user",
                    "content": "Greet the user warmly and introduce yourself as a customer service agent for Disha Communications. Then ask for their name, their designation, and where they're calling from. Once you have this information, ask for their phone number and email address. Be friendly and professional. Keep each question brief and wait for their response before moving to the next question."
                }
            ]
        )

        # Use context aggregator for proper conversation flow
        context_aggregator = LLMContextAggregatorPair(context)

        # Build pipeline with context aggregator
        pipeline = Pipeline(
            [
                transport.input(),
                context_aggregator.user(),
                llm,
                transport.output(),
                context_aggregator.assistant(),
            ]
        )

        # Create task
        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=16000,
                audio_out_sample_rate=16000,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
        )

        # Set up event handlers
        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            logger.info(f"First participant joined: {participant}")
            # Start capturing transcription for the participant
            await transport.capture_participant_transcription(participant["id"])
            
            # Give a moment for audio to be ready, then start conversation
            await asyncio.sleep(0.5)
            try:
                # Use LLMRunFrame to immediately trigger the LLM with the initial context
                await task.queue_frames([LLMRunFrame()])
                logger.info("Initial greeting triggered with LLMRunFrame")
            except Exception as e:
                logger.error(f"Error sending greeting: {e}")

        @transport.event_handler("on_participant_left")
        async def on_participant_left(transport, participant, reason):
            logger.info(f"Participant left: {participant}, reason: {reason}")
            await task.queue_frame(EndFrame())

        @transport.event_handler("on_participant_joined")
        async def on_participant_joined(transport, participant):
            logger.info(f"Participant joined: {participant}")
            # Capture transcription for any new participant
            await transport.capture_participant_transcription(participant["id"])

        logger.info("Starting pipeline runner")
        runner = PipelineRunner()
        await runner.run(task)
        
        logger.info("Pipeline runner completed")

    except Exception as e:
        logger.error(f"Error running bot: {e}", exc_info=True)
        raise
    finally:
        if transport:
            try:
                logger.info("Cleaning up transport")
                await transport.cleanup()
            except Exception as e:
                logger.error(f"Error cleaning up transport: {e}")


@app.post("/start")
async def start_session(request: Request):
    """Create a Daily room, start the bot, and return connection details"""
    try:
        logger.info("Creating Daily room and starting bot...")
        
        # Create room and token
        room_url, token = await create_daily_room()
        logger.info(f"Created room: {room_url}")

        # Start bot in background
        asyncio.create_task(run_bot(room_url, token))

        # Return connection details
        return JSONResponse(
            content={
                "room_url": room_url,
                "token": token,
            }
        )

    except Exception as e:
        logger.error(f"Error starting session: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": str(e)},
        )


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("PORT", "8002"))
    
    # Start ngrok tunnel before starting the server
    try:
        public_url = start_ngrok_tunnel(port)
        logger.info("🎉 Bot is ready to accept connections!")
        logger.info(f"📋 Make POST requests to: {public_url}/start")
    except Exception as e:
        logger.error(f"Failed to start ngrok tunnel: {e}")
        logger.warning("⚠️  Continuing without ngrok. Bot will only be accessible locally.")
    
    # Start the FastAPI server
    uvicorn.run(app, host="0.0.0.0", port=port)
