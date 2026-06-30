"""Task tool for delegating subtasks to subagents."""


def get_tool_definition():
    return {
        "type": "function",
        "function": {
            "name": "create_new_task",
            "description": "Delegate a subtask to a specialized subagent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "The task description for the subagent.",
                    },
                    "subagent_type": {
                        "type": "string",
                        "description": "The type of subagent to use.",
                    },
                    "thoroughness_level": {
                        "type": "integer",
                        "description": "Thoroughness level from 1 (quick) to 5 (exhaustive).",
                        "minimum": 1,
                        "maximum": 5,
                    },
                },
                "required": ["task", "subagent_type", "thoroughness_level"],
            },
        },
    }


def execute_task(task, subagent_type, thoroughness_level):
    raise NotImplementedError("execute_task not yet implemented")
