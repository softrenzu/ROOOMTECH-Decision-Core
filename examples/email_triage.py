import requests

payload = {
    "input": "Customer says the product is broken and wants a refund today.",
    "provider": "auto",
    "decisions": [
        {
            "id": "category",
            "question": "What type of message is this?",
            "choices": ["thanks", "question", "complaint", "other"],
            "keywords": {"complaint": ["broken", "refund"]},
        },
        {
            "id": "reply_needed",
            "question": "Does this require a reply?",
            "choices": ["yes", "no"],
            "keywords": {"yes": ["refund", "question", "help"]},
        },
    ],
}

response = requests.post("http://localhost:8000/v1/decide", json=payload, timeout=10)
response.raise_for_status()
print(response.json())
