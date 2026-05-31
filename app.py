import os
import json
import requests
import threading
import weave
from flask import Flask, request, jsonify
from openai import OpenAI
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# 1. INITIALIZE APPS & TRACKING PIPELINES
app = Flask(__name__)
SCOPES = ['https://www.googleapis.com/auth/calendar']

# Track your entire architecture lifecycle inside your Weights & Biases sandbox
weave.init("ansh-kanungous25-northeastern-university/nyc-trip-orchestrator")

# 2. CREDENTIAL MANAGEMENT CONFIGURATION
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
WANDB_API_KEY = os.environ.get("WANDB_API_KEY", "")
TICKETMASTER_API_KEY = os.environ.get("TICKETMASTER_API_KEY", "")

# Initialize API Clients
slack_client = WebClient(token=SLACK_BOT_TOKEN)
client = OpenAI(
    base_url="https://api.inference.wandb.ai/v1", 
    api_key=WANDB_API_KEY
)

# LLM Deployment Route Configuration
LEAD_MODEL = "meta-llama/Llama-3.3-70B-Instruct"
WORKER_MODEL = "deepseek-ai/DeepSeek-V4-Flash"

# --- 3. GLOBAL IN-MEMORY TRANSACTION CACHES ---
TRIP_METADATA_CACHE = {}
USER_OPTIONS_CACHE = {}


# --- 4. DATA PIPELINE UTILITIES & SLACK SINK ---
def get_calendar_service():
    """Authenticates the user via OAuth2 and returns the Google Calendar service."""
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('calendar', 'v3', credentials=creds)


def send_slack_message(channel_id: str, text: str):
    """Dispatches payload targets directly back into the conversational workspace."""
    try:
        slack_client.chat_postMessage(channel=channel_id, text=text)
    except SlackApiError as e:
        print(f"⚠️ Slack message failed: {e.response['error']}")
        print(f"BOT WOULD HAVE SAID:\n{text}")


@weave.op()
def fetch_live_events(city: str, date_str: str) -> list:
    """Queries Ticketmaster live discovery services for verified regional events."""
    start_dt = f"{date_str}T00:00:00Z"
    end_dt = f"{date_str}T23:59:59Z"
    
    url = (
        f"https://app.ticketmaster.com/discovery/v2/events.json"
        f"?apikey={TICKETMASTER_API_KEY}&city={city}&startDateTime={start_dt}&endDateTime={end_dt}&size=8"
    )
    try:
        res = requests.get(url, timeout=10).json()
        raw_events = res.get("_embedded", {}).get("events", [])
        
        clean_list = []
        for e in raw_events:
            clean_list.append({
                "name": e.get("name", "Special Event")[:50],
                "time": e.get("dates", {}).get("start", {}).get("localTime", "TBD")
            })
        return clean_list
    except Exception as e:
        print(f"⚠️ Live API Fetch Interrupted: {e}")
        return []


# --- 5. AGENTIC ENGINE DEPLOYMENT METHODS ---
@weave.op()
def lead_router_agent(user_prompt: str) -> dict:
    """Translates user intents across multiple languages and extracts core tokens."""
    system_instruction = (
        "You are an elite multilingual travel extraction agent. The user can talk to you in any language "
        "(English, Spanish, Hindi, Gujarati, etc.). Translate their intent to English internally and extract "
        "the destination city, ISO target date (YYYY-MM-DD), and an array of thematic filter tokens.\n"
        "If they seek current concerts, festivals, games, or live local happenings, you MUST add 'city_specials' to the array.\n\n"
        "Respond ONLY with a valid JSON object matching this schema:\n"
        '{"destination": "City Name", "travel_date": "YYYY-MM-DD", "filters": ["filter1", "city_specials"]}'
    )
    response = client.chat.completions.create(
        model=LEAD_MODEL,
        messages=[{"role": "system", "content": system_instruction}, {"role": "user", "content": user_prompt}],
        response_format={"type": "json_object"}
    )
    return json.loads(response.choices[0].message.content)


@weave.op()
def generate_short_options(destination: str, filter_topic: str) -> list:
    """Generates standard contextual choices using deep internal model weights."""
    prompt = (
        f"Provide exactly 5 high-quality activity recommendations for '{filter_topic}' in {destination}. "
        "Each option must be short, highly relevant, and include an ideal time slot and a brief 1-sentence description.\n"
        "Respond ONLY with a JSON object matching this exact schema:\n"
        '{"options": [\n'
        '  {"name": "Place Name", "time": "10:00 AM", "one_liner": "Short descriptive sentence."}\n'
        ']}'
    )
    response = client.chat.completions.create(
        model=WORKER_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=350
    )
    return json.loads(response.choices[0].message.content).get("options", [])


@weave.op()
def generate_deep_description(activity_name: str) -> str:
    """Uses the worker model to write a detailed summary of the location."""
    prompt = f"Write an engaging, detailed 2-3 sentence travel guide description for '{activity_name}'. Focus on what makes it special and why a traveler should visit."
    response = client.chat.completions.create(
        model=WORKER_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200
    )
    return response.choices[0].message.content


@weave.op()
def format_for_calendar(option_names: list, travel_dates: str) -> list:
    """Converts the list of names into a strict JSON array for Google Calendar."""
    prompt = (
        f"I am planning a trip on these dates: {travel_dates}. "
        f"Convert these places into calendar events: {', '.join(option_names)}. "
        "Assume the year is 2026. Space the events out logically. "
        "Respond ONLY with a JSON object containing an 'events' array matching this exact schema:\n"
        '{"events": [{"summary": "Event Name", "description": "Details", "start_time": "YYYY-MM-DDTHH:MM:SS", "end_time": "YYYY-MM-DDTHH:MM:SS"}]}'
    )
    response = client.chat.completions.create(
        model=LEAD_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )
    return json.loads(response.choices[0].message.content).get("events", [])


# --- 6. ON-THE-FLY AGENTIC A/B EVALUATORS ---
@weave.op()
def agentic_prompt_writer(destination: str, raw_data_str: str) -> dict:
    """Prompt Engineer Agent: Generates competing instructions to structure the raw payload."""
    sys_prompt = (
        "You are an expert AI prompt engineer. Draft two distinct instructions (prompt_a and prompt_b) "
        "directing a model to parse messy API JSON into exactly 5 clean single-line UI choices.\n"
        "Prompt A should focus on natural phrasing. Prompt B should prioritize strict token compression "
        "and keeping the text under 50 characters.\n"
        "You MUST respond with a JSON object matching this exact schema: {\"prompt_a\": \"...\", \"prompt_b\": \"...\"}"
    )
    user_content = f"Target Destination: {destination}. Sample context data: {raw_data_str}"
    response = client.chat.completions.create(
        model=LEAD_MODEL,
        messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_content}],
        response_format={"type": "json_object"}
    )
    return json.loads(response.choices[0].message.content)


@weave.op()
def evaluate_and_format_payload(prompt_variant: str, raw_data_str: str) -> tuple:
    """Executes a target variant prompt and scores compliance against interface limits."""
    full_instruction = (
        f"{prompt_variant}\n\nRAW INPUT TO PARSE:\n{raw_data_str}\n\n"
        "Respond strictly in JSON matching: {'options': [{'name': 'X', 'time': 'Y', 'one_liner': 'Z'}]}"
    )
    response = client.chat.completions.create(
        model=WORKER_MODEL,
        messages=[{"role": "user", "content": full_instruction}],
        response_format={"type": "json_object"}
    )
    result_text = response.choices[0].message.content
    tokens_used = response.usage.total_tokens
    
    # Mathematical Scoring Logic Matrix
    score = 0
    try:
        parsed_options = json.loads(result_text).get("options", [])
        if len(parsed_options) == 5: 
            score += 50
        if all(len(item.get("one_liner", "")) <= 50 for item in parsed_options): 
            score += 50
    except Exception:
        score = 0
        
    return score, tokens_used, result_text


# --- 7. CORE COMPILER ASYNC BACKGROUND FLOWS ---
def background_generation_task(user_message: str, channel_id: str):
    """Processes queries asynchronously, executes A/B evaluation rounds, and prints to Slack."""
    print(f"⚙️ Intercepted input stream processing: '{user_message}'")
    
    # Process multi-language string via the lead router
    plan_meta = lead_router_agent(user_message)
    
    # Safely extract with fallbacks to prevent NoneType errors
    destination = plan_meta.get("destination") or "Unknown Location"
    travel_date = plan_meta.get("travel_date") or "TBD"
    filters = plan_meta.get("filters") or []
    
    TRIP_METADATA_CACHE['dates'] = travel_date
    
    global USER_OPTIONS_CACHE
    USER_OPTIONS_CACHE.clear()
    
    counter = 1
    final_text = f"✨ *Travel Options for {destination.title()} ({travel_date})* ✨\n"
    
    for f in filters:
        filter_label = f.upper().replace('_', ' ')
        final_text += f"\n📍 *Category: {filter_label}*\n"
        
        # Branch Execution based on data type (Live API vs Internal Knowledge)
        if f == "city_specials":
            # Fetch verified data from live API source
            live_data = fetch_live_events(destination, travel_date)
            live_data_str = json.dumps(live_data)
            
            # Agentic Loop: Generate competing system formatting configurations
            engineered_prompts = agentic_prompt_writer(destination, live_data_str)
            p_a = engineered_prompts.get("prompt_a", "")
            p_b = engineered_prompts.get("prompt_b", "")
            
            # Evaluate constraints dynamically
            score_a, tokens_a, out_a = evaluate_and_format_payload(p_a, live_data_str)
            score_b, tokens_b, out_b = evaluate_and_format_payload(p_b, live_data_str)
            
            # Select the most efficient formatting payload strategy
            winning_json_str = out_b if (score_b >= score_a and tokens_b <= tokens_a) else out_a
            
            try:
                options = json.loads(winning_json_str).get("options", [])
            except Exception:
                options = [{"name": "Live Event Hub", "time": "7:00 PM", "one_liner": "Check local ticketing access windows."}]
        else:
            # Standard worker generation routing
            options = generate_short_options(destination, f)
            
        # Standardize items into single-line presentation blocks
        for opt in options:
            name = opt.get("name", "Activity Item")
            time_slot = opt.get("time", "TBD")
            desc = opt.get("one_liner", "")
            
            USER_OPTIONS_CACHE[counter] = name
            final_text += f"*[{counter}]* {name} ({time_slot}) — _{desc}_\n"
            counter += 1
            
    final_text += "\n\nReply with your choices (e.g., `add 1 2`) to build your itinerary, or `info 1` for details!"
    send_slack_message(channel_id, final_text)


def fetch_description_task(option_num: int, activity_name: str, channel_id: str):
    """Background thread to fetch details and send to Slack."""
    details = generate_deep_description(activity_name)
    send_slack_message(channel_id, f"📖 *Details for [{option_num}] {activity_name}*\n{details}")


def add_to_calendar_task(selected_names: list, channel_id: str):
    """Background thread to structure events and push to Google Calendar."""
    dates = TRIP_METADATA_CACHE.get('dates', '2026-06-06')
    cal_events = format_for_calendar(selected_names, dates)
    
    service = get_calendar_service()
    for item in cal_events:
        try:
            event_body = {
                'summary': item.get('summary', 'Travel Activity'),
                'description': item.get('description', ''),
                'start': {'dateTime': item.get('start_time'), 'timeZone': 'America/New_York'},
                'end': {'dateTime': item.get('end_time'), 'timeZone': 'America/New_York'},
            }
            service.events().insert(calendarId='primary', body=event_body).execute()
        except Exception as e:
            print(f"❌ Failed to log event '{item.get('summary')}': {e}")
            
    send_slack_message(channel_id, f"✅ Successfully added {len(selected_names)} items to your Google Calendar!")


# --- 8. FLASK ACTION EVENT LISTENER GATEWAY ---
@app.route("/slack/events", methods=["POST"])
def slack_events():
    """Intercepts and routes incoming Slack messages based on intent."""
    data = request.json
    
    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]}), 200
        
    event = data.get("event", {})
    user_message = event.get("text", "").strip().lower()
    channel_id = event.get("channel")
    
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return jsonify({"status": "ignored"}), 200
        
    if user_message:
        # 1. COMMAND CHECK: Description/Info request
        detail_commands = ("desc ", "info ", "details ", "more ", "expand ", "show ")
        
        if user_message.startswith(detail_commands):
            try:
                command_used = next(cmd for cmd in detail_commands if user_message.startswith(cmd))
                option_num = int(user_message.replace(command_used, "").strip())
                activity_name = USER_OPTIONS_CACHE.get(option_num)
                
                if activity_name:
                    send_slack_message(channel_id, f"🔍 *Fetching deeper details for [{option_num}] {activity_name}...*")
                    threading.Thread(target=fetch_description_task, args=(option_num, activity_name, channel_id)).start()
                else:
                    send_slack_message(channel_id, f"⚠️ Couldn't find option `[{option_num}]` in your recent search cache.")
            except ValueError:
                send_slack_message(channel_id, "⚠️ Invalid format. Use a command followed by the number (e.g., `info 2`).")
            return jsonify({"status": "handled_description"}), 200

        # 2. COMMAND CHECK: Calendar selection (e.g., "7", "1 2 4", or "add 1 2")
        cleaned_msg = user_message.replace("add", "").replace(",", " ").strip()
        if cleaned_msg.replace(" ", "").isdigit() and cleaned_msg != "":
            choices = [int(x) for x in cleaned_msg.split() if x.isdigit()]
            
            selected_names = [USER_OPTIONS_CACHE[num] for num in choices if num in USER_OPTIONS_CACHE]
            
            if selected_names:
                send_slack_message(channel_id, f"📅 *Adding to calendar:* {', '.join(selected_names)}")
                threading.Thread(target=add_to_calendar_task, args=(selected_names, channel_id)).start()
            else:
                send_slack_message(channel_id, "⚠️ None of those numbers matched your current options.")
            return jsonify({"status": "handled_itinerary_selection"}), 200

        # 3. DEFAULT ROUTE: Treat it as a multi-language trip request sentence
        threading.Thread(
            target=background_generation_task, 
            args=(event.get("text").strip(), channel_id)
        ).start()
        
    return jsonify({"status": "processing"}), 200


if __name__ == "__main__":
    print("🔐 Verifying Google Calendar connection...")
    get_calendar_service()
    
    print("🌐 Launching Agent Webhook Server on Port 5000...")
    app.run(port=5000)