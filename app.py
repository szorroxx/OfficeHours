import os
from flask import Flask, render_template, request, jsonify
from openai import OpenAI
from dotenv import load_dotenv

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
def prompt_nemotron():
    data = request.get_json()
    user_input = data.get("prompt", "")

    if not user_input:
        return jsonify({"error": "No prompt provided"}), 400

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": user_input + "\n\nPlease only response with a snippet of HTML code that can be embedded into a HTML div. Do not include any text outside of the HTML code. Do not include any script tags or JavaScript code. Do not include any CSS code. Only provide the HTML code. Make sure the HTML code is valid and can be embedded into a div. Make sure the HTML code is responsive and works well on different screen sizes. Make sure the HTML code is accessible and follows best practices for web accessibility. Make sure the final HTML code is easily readable by a human when it's rendered."}],
        )
        answer = response.choices[0].message.content
        return jsonify({"response": answer})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
