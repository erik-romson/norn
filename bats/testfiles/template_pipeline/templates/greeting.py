from norn.templates import PromptTemplate

greeting = PromptTemplate(
    name="greeting",
    template="Greet the following person: {input}",
    system_prompt="You are a friendly greeter.",
)
