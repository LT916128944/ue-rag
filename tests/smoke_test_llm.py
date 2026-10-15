# scripts/smoke_test_llm.py
import os
import json
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI
from core.tool_router import build_agent_prompt, retrieve_tools

api_key = os.getenv("DEEPSEEK_API_KEY")
client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

# 用这个全局变量模拟 UE 编辑器里的真实状态
MOCK_ACTORS = {
    "EnemySpawnPoint_1": {"location": {"x": 0, "y": 0, "z": 100}},
    "EnemySpawnPoint_2": {"location": {"x": 200, "y": 0, "z": 100}},
}

def execute_tool(tool_name: str, arguments: str) -> str:
    args = json.loads(arguments)
    print(f"\n  >> [Mock UE] 执行工具: {tool_name}, 参数: {args}")
    
    if tool_name == "find_actors":
        return json.dumps([{"name": k, "location": v["location"]} for k, v in MOCK_ACTORS.items()])
        
    elif tool_name == "get_actor_transform":
        actor_name = args.get("actor", {}).get("refPath", "")
        if actor_name in MOCK_ACTORS:
            return json.dumps(MOCK_ACTORS[actor_name])
        return json.dumps({"error": f"Actor {actor_name} not found"})
        
    elif tool_name == "set_actor_transform":
        actor_name = args.get("actor", {}).get("refPath", "")
        new_xform = args.get("xform", {})
        if actor_name in MOCK_ACTORS:
            if "location" in new_xform:
                MOCK_ACTORS[actor_name]["location"] = new_xform["location"]
            return json.dumps({"success": True})
        return json.dumps({"error": f"Actor {actor_name} not found"})
        
    return json.dumps({"error": "Unknown tool"})

def run_agent_loop():
    user_query = "把场景里所有敌人的出生点 Z 坐标提升 100 并验证"
    prompt = build_agent_prompt(user_query, "项目类: AEnemySpawnPoint")
    tools = retrieve_tools(user_query, top_k=5)

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_query}
    ]

    # 多轮循环，最多执行 10 步，防止死循环
    for step in range(10):
        print(f"\n--- Step {step + 1} ---")
        
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            tools=tools,
            tool_choice="auto"
        )
        message = response.choices[0].message
        messages.append(message)  # 把 LLM 的回复加入对话历史

        # 如果没有工具调用，说明 LLM 认为任务完成了
        if not message.tool_calls:
            print("\n=== ✅ 任务完成，LLM 最终回复 ===")
            print(message.content)
            break

        # 如果有工具调用，逐个执行，并把结果喂回
        for tool_call in message.tool_calls:
            result = execute_tool(tool_call.function.name, tool_call.function.arguments)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result
            })
            print(f"  << [Observe] 结果已返回: {result[:100]}...")
    else:
        print("\n⚠️ 达到最大步数限制，任务可能未完成。")

if __name__ == "__main__":
    run_agent_loop()