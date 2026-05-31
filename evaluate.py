import asyncio
import weave
from agent import search_french_restaurants

weave.init("nyc-trip-orchestrator")

# 1. Create a quick Hackathon Dataset
dataset = [
    {"location": "New York City", "expected_keyword": "Balthazar"},
    {"location": "New York City", "expected_keyword": "Le Bernardin"},
]

# 2. Create a Scorer (Did Claude mention the famous restaurants we expected?)
@weave.op()
def keyword_scorer(expected_keyword: str, model_output: dict) -> dict:
    # model_output is the string returned by our agent
    success = expected_keyword.lower() in model_output.lower()
    return {"correct": success}

async def main():
    # 3. Run the Evaluation
    evaluation = weave.Evaluation(
        name="restaurant_prompt_v1",
        dataset=dataset,
        scorers=[keyword_scorer]
    )
    
    print("Running evaluation...")
    results = await evaluation.evaluate(search_french_restaurants)
    print("Evaluation complete! Check your W&B dashboard.")

if __name__ == "__main__":
    asyncio.run(main())