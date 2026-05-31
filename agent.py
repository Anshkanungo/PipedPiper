import os
import json
import weave
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor

# 1. Initialize Weave and point the OpenAI SDK to W&B Inference
# NOTE: Make sure to keep your API key secure!
os.environ["WANDB_API_KEY"] = "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"
os.environ["WANDB_ENTITY"] = "ansh-kanungous25-northeastern-university"

weave.init("nyc-trip-orchestrator")

client = OpenAI(
    base_url="https://api.inference.wandb.ai/v1", 
    api_key=os.environ["WANDB_API_KEY"] 
)

# Model allocations based on reasoning intensity
LEAD_MODEL = "meta-llama/Llama-3.3-70B-Instruct"    # High reasoning router, writer, and critic
WORKER_MODEL = "deepseek-ai/DeepSeek-V4-Flash"     # Fast, ultra-cheap execution machine

@weave.op()
def lead_router_agent(user_prompt: str) -> dict:
    """Parses raw text into crisp, distinct filter tasks using the heavy model."""
    system_instruction = (
        "You are a master travel orchestrator. Break down the user prompt into structured metadata. "
        "Extract the destination, travel dates, and a distinct array of targeted lookup filters. "
        "You must respond ONLY with a valid JSON object matching this schema exactly:\n"
        '{"destination": "string", "travel_dates": "string", "filters": ["string", "string"]}'
    )
    
    response = client.chat.completions.create(
        model=LEAD_MODEL,
        messages=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt}
        ],
        response_format={"type": "json_object"}
    )
    return json.loads(response.choices[0].message.content)

@weave.op()
def prompt_generator_agent(destination: str, filter_topic: str, previous_feedback: str = "") -> str:
    """Generates a highly customized search prompt for sub-agents, iterating if given feedback."""
    system_instruction = (
        "You are an expert prompt engineer specialized in AI travel research agents. "
        "Your task is to write a highly detailed, targeted 2-sentence prompt that tells a worker agent "
        "exactly what kind of locations to find. Focus heavily on context, mood, and explicit constraints."
    )
    
    user_content = f"Write a research prompt to find exactly 3 premium recommendations for '{filter_topic}' in {destination}."
    
    # If the previous attempt failed, feed the critic's analysis back in to fix it
    if previous_feedback:
        user_content += (
            f"\n\nCRITICAL CRITIQUE FROM PREVIOUS ATTEMPT:\n{previous_feedback}\n"
            "Analyze where your previous prompt allowed room for error, and rewrite the new prompt "
            "to explicitly fix these flaws."
        )
        
    response = client.chat.completions.create(
        model=LEAD_MODEL,
        messages=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_content}
        ]
    )
    return response.choices[0].message.content

@weave.op()
def execute_generated_prompt(custom_prompt: str) -> str:
    """Executes the dynamically generated prompt using the fast, cheap worker model."""
    response = client.chat.completions.create(
        model=WORKER_MODEL,
        messages=[{"role": "user", "content": custom_prompt}],
        max_tokens=400
    )
    return response.choices[0].message.content

@weave.op()
def evaluator_agent(filter_topic: str, destination: str, worker_output: str) -> dict:
    """Acts as a harsh critic evaluating the output. Returns structured pass/fail analytics."""
    system_instruction = (
        "You are an elite travel quality inspector. Evaluate the worker's output thoroughly. "
        "Check for: \n"
        "1. Did they return exactly 3 options?\n"
        "2. Are the options highly accurate and matching the topic requested?\n"
        "3. Are the places physically located within the destination city?\n"
        "You must respond ONLY with a valid JSON object matching this schema exactly:\n"
        '{"pass": true/false, "feedback": "Detailed explanation of what was good or what needs fixing."}'
    )
    
    user_content = (
        f"Target Topic: {filter_topic}\n"
        f"Expected Location: {destination}\n"
        f"Worker Output:\n{worker_output}"
    )
    
    response = client.chat.completions.create(
        model=LEAD_MODEL,
        messages=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_content}
        ],
        response_format={"type": "json_object"}
    )
    return json.loads(response.choices[0].message.content)

@weave.op()
def smart_worker_pipeline(destination: str, filter_topic: str) -> str:
    """Manages the optimization loop: writes prompts, runs workers, and listens to the critic."""
    max_retries = 3
    feedback = ""
    
    for attempt in range(max_retries):
        print(f"🔄 [{filter_topic}] Prompt generation attempt {attempt + 1}/{max_retries}...")
        
        # 1. Generate an optimized prompt
        optimized_prompt = prompt_generator_agent(destination, filter_topic, feedback)
        
        # 2. Execute it via the cheap model
        worker_output = execute_generated_prompt(optimized_prompt)
        
        # 3. Judge the quality of the execution
        critique = evaluator_agent(filter_topic, destination, worker_output)
        
        if critique.get("pass", False):
            print(f"✅ [{filter_topic}] Passed evaluation on attempt {attempt + 1}!")
            return worker_output
        else:
            feedback = critique.get("feedback", "Output did not meet standard criteria.")
            print(f"⚠️ [{filter_topic}] Failed evaluation. Reason: {feedback}")
            
    print(f"🚨 [{filter_topic}] Hit max retries. Returning best available output.")
    return worker_output

@weave.op()
def final_compiler_agent(metadata: dict, worker_outputs: list) -> str:
    """Assembles all concurrently processed options into a beautiful final itinerary layout."""
    itinerary = f"✨ **Custom Itinerary for {metadata['destination']}** ({metadata['travel_dates']}) ✨\n\n"
    for filter_name, recommendations in worker_outputs:
        itinerary += f"### 📍 {filter_name.title()}\n{recommendations}\n\n"
    return itinerary

@weave.op()
def run_orchestration_workflow(slack_message: str) -> str:
    """Main execution block managing the data flow and concurrency."""
    print("🧠 Activating Lead Agent to analyze request...")
    plan_meta = lead_router_agent(slack_message)
    
    destination = plan_meta.get("destination", "New York City")
    filters = plan_meta.get("filters", [])
    print(f"📋 Extracted Tasks: {filters}")
    
    print(f"🚀 Launching self-optimizing pipelines in parallel...")
    worker_results = []
    
    # Run the smart pipelines concurrently to maintain low latency for Slack
    with ThreadPoolExecutor(max_workers=len(filters)) as executor:
        future_to_filter = {
            executor.submit(smart_worker_pipeline, destination, f): f 
            for f in filters
        }
        for future in future_to_filter:
            filter_name = future_to_filter[future]
            worker_results.append((filter_name, future.result()))
            
    print("🎨 Compiling dynamic worker data into final response...")
    final_response = final_compiler_agent(plan_meta, worker_results)
    return final_response

if __name__ == "__main__":
    sample_slack_input = "I am thinking of a 2 day NYC trip on 23rd of july, i am interesting in sky-views and french restaurants, create me some itenary plans."
    
    output = run_orchestration_workflow(sample_slack_input)
    print("\n--- Final Consolidated Output ---\n")
    print(output)