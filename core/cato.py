from tools.file_search import search_files


class Cato:
    def __init__(self):
        self.name = "Cato"

    def respond(self, command: str) -> str:
        command = command.strip()

        if not command:
            return "I didn't catch that."

        # First real capability: file search
        if "find" in command.lower() and "file" in command.lower():
            query = self._extract_search_query(command)

            if not query:
                return "What should I search for?"

            results = search_files(query)

            if not results:
                return f"I couldn't find anything matching '{query}'."

            response = f"I found {len(results)} result(s):\n"

            for index, path in enumerate(results, start=1):
                response += f"{index}. {path}\n"

            return response.rstrip()

        return f"I understand your command, but I don't have a tool for it yet: {command}"

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

    print("Cato v0.2")
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
