import os
from flask import Flask, render_template, request, jsonify
from openai import OpenAI
from dotenv import load_dotenv
from nat.runtime.loader import load_workflow

load_dotenv()  # reads .env file into environment variables

app = Flask(__name__)

# Nemotron client, pointed at NVIDIA's hosted API
client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=os.environ["NVIDIA_API_KEY"],
)

MODEL_NAME = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/prompt", methods=["POST"])
async def prompt_nemotron():
    data = request.get_json()
    user_input = data.get("prompt", "")

    if not user_input:
        return jsonify({"error": "No prompt provided"}), 400

    try:
        async with load_workflow("config.yml") as workflow:
            async with workflow.run(user_input) as runner:
                result = await runner.result(to_type=str)
        return jsonify({"response": result})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
