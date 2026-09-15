from tools.file_search import search_files
from core.tool_registry import ToolRegistry


class Cato:
    def __init__(self):
        self.name = "Cato"

        self.tools = ToolRegistry()
        self.tools.register("file_search", search_files)

    def respond(self, command: str) -> str:
        command = command.strip()

        if not command:
            return "I didn't catch that."

        if "find" in command.lower() and "file" in command.lower():
            query = self._extract_search_query(command)

            if not query:
                return "What should I search for?"

            results = self.tools.run("file_search", query=query)

            if not results:
                return f"I couldn't find anything matching '{query}'."

            response = f"I found {len(results)} result(s):\n"

            for index, path in enumerate(results, start=1):
                response += f"{index}. {path}"

                if index < len(results):
                    response += "\n"

            return response

        return (
            "I understand your command, but I don't have a tool "
            f"for it yet: {command}"
        )

    def _extract_search_query(self, command: str) -> str:
        command = command.lower()

        prefixes = [
            "find my ",
            "find the ",
            "find ",
            "search for my ",
            "search for the ",
            "search for ",
        ]

        for prefix in prefixes:
            if command.startswith(prefix):
                query = command[len(prefix):].strip()

                if query.endswith(" files"):
                    query = query[:-6].strip()

                return query

        return ""


def main():
    cato = Cato()

    print("Cato v0.3")
    print("Type 'exit' to shut down.\n")

    while True:
        command = input("You: ")

        if command.lower() in {"exit", "quit"}:
            print("Cato: Shutting down.")
            break

        response = cato.respond(command)
        print(f"Cato: {response}\n")


if __name__ == "__main__":
    main()
