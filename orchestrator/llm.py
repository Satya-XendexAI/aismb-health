import json
import os
from typing import List
from openai import OpenAI
from dotenv import load_dotenv

from models.session import (
    ChatTurn, ChatRole, AgentResponse, AgentResponseType, ToolCall,
)
from prompts.system import PATIENT_SYSTEM_PROMPT

load_dotenv()


class GeminiLLMAdapter:
    def __init__(
        self,
        model:    str = os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
        base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key:  str = os.getenv("GEMINI_API_KEY", ""),
    ):
        self.model  = model
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=120.0)

    def run_agent(self, history: List[ChatTurn], tool_schemas: list, system_prompt: str = PATIENT_SYSTEM_PROMPT) -> AgentResponse:
        messages = [{"role": "system", "content": system_prompt}] + self._build_messages(history)

        completion = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tool_schemas,
            tool_choice="auto",
            temperature=float(os.getenv("GEMINI_TEMPERATURE", "1.0")),
            max_tokens=8192,
            stream=False,
        )

        choice        = completion.choices[0]
        finish_reason = choice.finish_reason
        message       = choice.message


        if finish_reason == "tool_calls" and message.tool_calls:
            tc  = message.tool_calls[0]
            sig = (tc.extra_content or {}).get("google", {}).get("thought_signature")
            return AgentResponse(
                type=AgentResponseType.TOOL_CALL,
                tool_call=ToolCall(
                    tool_name=tc.function.name,
                    args=json.loads(tc.function.arguments),
                    tool_use_id=tc.id,
                    thought_signature=sig,
                ),
            )

        return AgentResponse(
            type=AgentResponseType.TEXT,
            text=message.content or "",
        )

    def _build_messages(self, history: List[ChatTurn]) -> list:
        messages = []
        for i, turn in enumerate(history):
            if turn.role == ChatRole.USER:
                if messages and messages[-1]["role"] == "user":
                    messages[-1]["content"] += "\n" + turn.content
                else:
                    messages.append({"role": "user", "content": turn.content})

            elif turn.role == ChatRole.ASSISTANT:
                if turn.tool_call:
                    tc_entry = {
                        "id":       turn.tool_call.tool_use_id,
                        "type":     "function",
                        "function": {
                            "name":      turn.tool_call.tool_name,
                            "arguments": json.dumps(turn.tool_call.args),
                        },
                    }
                    if turn.tool_call.thought_signature:
                        tc_entry["extra_content"] = {
                            "google": {"thought_signature": turn.tool_call.thought_signature}
                        }
                    messages.append({
                        "role":       "assistant",
                        "content":    None,
                        "tool_calls": [tc_entry],
                    })
                else:
                    messages.append({"role": "assistant", "content": turn.content or " "})

            elif turn.role == ChatRole.TOOL_RESULT:
                # Prefer the tool_call stored directly on this turn (reliable)
                if turn.tool_call:
                    tool_use_id = turn.tool_call.tool_use_id
                    tool_name   = turn.tool_call.tool_name
                else:
                    # Fallback: search backwards for the matching ASSISTANT tool call
                    tool_use_id = "unknown"
                    tool_name   = "unknown"
                    for prev_turn in reversed(history[:i]):
                        if prev_turn.role == ChatRole.ASSISTANT and prev_turn.tool_call:
                            tool_use_id = prev_turn.tool_call.tool_use_id
                            tool_name   = prev_turn.tool_call.tool_name
                            break
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tool_use_id,
                    "name":         tool_name,
                    "content":      turn.content,
                })

        return messages

class PrintWANotifier:
    def send(self, to_number: str, text: str):
        print(f"\n  Bot >> {text}\n")
