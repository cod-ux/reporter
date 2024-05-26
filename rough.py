import os
import toml
import asyncio
import operator

from langchain_openai import ChatOpenAI
from langchain_core.pydantic_v1 import BaseModel, Field
from langchain.chains.openai_functions import create_structured_output_runnable
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, END

import phoenix as px
from phoenix.trace.langchain import LangChainInstrumentor
from typing import TypedDict, List, Tuple, Annotated
from agent_templates import (
    planner_template,
    format_request,
    format_code_request,
    code_chain_template,
)


# OpenAI init.

secrets = "/Users/suryaganesan/Documents/GitHub/Replicate/secrets.toml"
github_secrets = "secrets.toml"
os.environ["OPENAI_API_KEY"] = toml.load(secrets)["OPENAI_API_KEY"]
llm = ChatOpenAI(temperature=0, model="gpt-4o")

# Phoenix init.

session = px.launch_app()
LangChainInstrumentor().instrument()


# Defining class objects for graph

class GraphState(TypedDict):
    request: str # Set
    source_path: str # Set

    tasks: List[str]
    past_tasks: List[str]
    current_task: str
    
    error: str
    messages: List[str]
    generation: str
    iterations: int # Set
    max_iterations: int # Set
    reflect: str

    response: str

class Plan(BaseModel):
    tasks: List[str] = Field(
        description="different tasks to complete to fulfill user request"
        )

class code(BaseModel):
    prefix: str = Field("Description of the problem and the code solution provided")
    imports: str = Field("Code block import statements")
    code: str = Field("Code block excluding import statements")
    description: str = "Schema for code solutions to execute openpyxl related user requests"


# Preparing code_chain()

prompt = code_chain_template()
code_chain = prompt | llm.with_structured_output(code)


# Node functions

async def plan_steps(state: GraphState):
    messages = state["messages"]
    past_tasks = state["past_tasks"]
    reflect = state["reflect"]
    past_tasks = []
    reflect = ''


    planner_prompt = planner_template()
    planner = create_structured_output_runnable(Plan, ChatOpenAI(model="gpt-4o", temperature=0, streaming=True), planner_prompt)

    print("\nPlanning step begins...\n")

    plan = await planner.ainvoke({"messages": state["messages"]})
    print(f"Planning step: \n, {plan.tasks}")
    plan_string = ''

    for i, task in enumerate(plan):
        plan_string += f'\n Task {i}: {task}'

    print(plan_string)

    messages += [('system', f"For the user request, this is the list of tasks I need to achieve to fulfill the request: \n{plan_string}")]
    
    return {"tasks": plan.tasks, "messages": messages, "past_tasks": past_tasks, "reflect": reflect}

# Function: Write code

async def generate_code(state: GraphState):
    messages = state["messages"]
    iterations = state["iterations"]
    error = state["error"]
    generation = state["generation"]
    tasks = state["tasks"]
    past_tasks = state["past_tasks"]

    plan_string = ''

    for i, task in enumerate(tasks):
        plan_string += f'\n Task {i}: {task}'

    print("Generating code....")

    if error == "yes":
        messages += [("system", f"I should try again to generate code with the existing plan without producing any errors. This is the plan to follow: \n{plan_string}")]

    for count, task in enumerate(tasks):

        print(f"\nCoding: {count}. {task}\n")
        code_request = format_code_request(task_to_be_executed=task)

        messages += [('system', f'{code_request}')]

        generation = await code_chain.ainvoke({"messages": messages})
        past_tasks += [f'{count}. {task}']
        past_tasks_string = ''

        for i, task in enumerate(past_tasks):
            past_tasks_string += f'\n Task {i}: {task}'

        messages += [("system", f"The last added step to the code was: {task}. \nThe Code solution so far is: \n{generation.imports} \n {generation.code}")]
        messages += [('system', f"So far the code written will be able to complete the following tasks from the plan: \n{past_tasks_string}")]

    iterations += 1
    print("Reached end of generation...")

    #last_msgs = messages[-3:]
    #last_indx = -3*len(task)
    #messages = messages[:last_indx] + last_msgs
    return {"generation": generation, "messages": messages, "iterations": iterations}

async def check_code(state: GraphState):
    print("Code is being checked....")
    messages = state["messages"]
    generation = state["generation"]
    error = state["error"]
    iterations = state["iterations"]
    reflect = state["reflect"]

    try:
        exec(generation.imports)

    except Exception as e:
        error = "yes"
        messages += [("system", f"Encountered an error with import statement: {e}")]

        print(f"Error with import statement: {e}")
        return {"error": error, "messages": messages}

    try:
        exec(generation.code)

    except Exception as e:
        error = "yes"
        messages += [("system", f"Encountered an error with code block statement: {e}")]

        print(f"Error with code block: {e}")
        return {"error": error, "messages": messages}

    error = "no"

    if iterations > 2:
        reflect = "yes"

    print("-----NO CODE TEST FAILURES-----")
    return {"messages": messages, "error": error, "reflect": reflect}

async def reflect_code(state: GraphState):
    messages = state["messages"]

    messages += [
        (
            'system',
            """
            I tried to solve the problem and failed a unit test. I need to reflect on this failure based on the generated error.\n
            Write a few key suggestions to avoid making the mistake again.
            """,
        )
    ]

    reflections = await code_chain.ainvoke({"messages": messages})
    messages += [("assistant", f"Here are reflections on the error: {reflections}")]

    return {"messages": messages}


def should_end(state: GraphState):
    error = state["error"]
    iterations = state["iterations"]
    max_iters = state["max_iterations"]
    reflect = state["reflect"]
    messages = state["messages"]

    if error == "no" or iterations == max_iters:
        print("----DECISION: FINISH----")
        print("Messages: \n", messages)
        return "end"
        

    else:
        print("----DECISION: RE-TRY SOLUTION----")
        if reflect == "yes":
            print("Reflecting on error...")
            return "reflect_code"

        else:
            return "generate"


print("\n Code successfully executed.\n")

wf = StateGraph(GraphState)

wf.add_node("plan", plan_steps)
wf.add_node("generate", generate_code)
wf.add_node("check_code", check_code)
wf.add_node("reflect_code", reflect_code)

wf.set_entry_point("plan")
wf.add_edge("plan", "generate")
wf.add_edge("generate", "check_code")
wf.add_conditional_edges(
    "check_code",
    should_end,
    {
        "end": END,
        "reflect_code": "reflect_code",
        "generate": "generate",
    }
)
wf.add_edge("reflect_code", "generate")

app = wf.compile()

print("\nApp starts work here....\n")

req = "reate a graph in the newsheet added based on the data in the first sheet. If you can't make the graph, print not possible."
source = '/Users/suryaganesan/vscode/ml/projects/reporter/pivot.xlsx'

formatted_req = format_request(req, source)
 
#response = app.invoke({"messages": [('user', formatted_req)], "request": req, "source_path": source, "max_iterations": 30, "iterations": 0})


inputs = {"messages": [('user', formatted_req)], "request": req, "source_path": source, "max_iterations": 30, "iterations": 0}

async def main(inputs=inputs):
    async for event in app.astream(inputs):
        for key, value in event.items():
            if key != "__END__":
                print(value)

asyncio.run(main())

print("\n......App ends work here.\n")

end_in = input("Successfully compiled. Quit?...")

