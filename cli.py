import time
from typing import Iterator

from textual import work
from textual.app import App, ComposeResult

from src.assistant.agent import AgentEvent, ask, ask_command, ask_error, generate_suggestions
from src.ui.header import StatusHeader
from src.ui.terminal import TerminalView
from src.ui.bottompanel import BottomPanel, SuggestionBar, TextArea


class TerminalApp(App):

    TITLE = "LTerm"
    CSS = """
    Screen {
        background: #000000;
        padding: 0;
        layout: vertical;
        overflow: hidden;
    }
    TerminalView {
        height: 1fr;
    }
    #bottom-dock {
        height: 10;
        width: 1fr;
    }
    """

    # UI

    def compose(self) -> ComposeResult:
        yield StatusHeader()
        yield TerminalView()
        yield BottomPanel(id="bottom-dock")

    # EVENTS

    def on_mount(self) -> None:
        self.query_one(TextArea).write("Welcome to LTerm! I'm your terminal learning assistant.")
        self.query_one(TextArea).write("use '-- <question>' to ask me a question.")
        self.query_one(TextArea).write("use '?? <command>' to get an explanation of a command.")
        self.query_one(TextArea).write("use 'exit' or 'bye' to exit the terminal.")
        self.query_one(SuggestionBar).add_suggestion("mkdir")

    def on_suggestion_bar_pressed(self, event: SuggestionBar.Pressed) -> None:
        terminal = self.query_one(TerminalView)
        terminal.set_prompt_input(event.text)
        self._explain_agent_command(event.text, terminal.agent_context())

    def on_terminal_view_command_executed(self, event: TerminalView.CommandExecuted) -> None:
        ok = event.exit_code == 0
        if ok and event.command.strip() == "clear":
            self.query_one(SuggestionBar).clear_suggestions()
            self.query_one(TextArea).clear()
            return
        if ok:
            icon = "✓ OK"
            lines = [
                icon,
                f"  Current User: {event.user}",
                f"  Current Dir:  {event.cwd}",
                f"  Last Command: {event.command}",
            ]
            if event.output:
                lines.append(f"  Output: {event.output}")
            self.query_one(TextArea).clear()
            self.query_one(TextArea).write("\n".join(lines))
        if not ok:
            error_info = (
                f"✗ ERROR\n"
                f"  Current User: {event.user}\n"
                f"  Current Dir:  {event.cwd}\n"
                f"  Last Command: {event.command}\n"
                + (f"  Output: {event.output}" if event.output else "")
            )
            self._explain_agent_error(event.command, error_info)

    def on_terminal_view_agent_queried(self, event: TerminalView.AgentQueried) -> None:
        if event.mode == "ask":
            self._answer_agent_question(event.query, event.context)
            return
        if event.mode == "command":
            self._explain_agent_command(event.query, event.context)
            return

        header = f"Explanation: {event.query}"
        lines = [header]
        if event.context:
            lines.append("")
            lines.append(event.context)
        self.query_one(TextArea).clear()
        self.query_one(TextArea).write("\n".join(lines))

    # AGENT

    def _render_agent_result(
        self,
        header: str,
        answer: str,
        suggestions: list[str],
        tool_lines: list[str],
    ) -> None:
        text_area = self.query_one(TextArea)
        suggestion_bar = self.query_one(SuggestionBar)

        lines = [header, ""]
        if tool_lines:
            lines.extend(tool_lines)
            lines.append("")
        lines.append(answer or "No answer returned.")

        text_area.clear()
        text_area.write("\n".join(lines))
        suggestion_bar.clear_suggestions()
        for suggestion in suggestions:
            suggestion_bar.add_suggestion(suggestion)

    def _format_tool_line(self, event: AgentEvent) -> str:
        arguments = (event.meta or {}).get("arguments", {})
        if not arguments:
            return f"Tool: {event.content}"
        parts = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
        return f"Tool: {event.content}({parts})"

    def _stream_agent_response(self, header: str, events_iter: Iterator[AgentEvent | str]) -> None:
        suggestion_bar = self.query_one(SuggestionBar)
        text_area = self.query_one(TextArea)

        suggestion_bar.clear_suggestions()
        text_area.clear()
        text_area.write(f"{header}\n\nThinking...")
        
        self._stream_agent_worker(header, events_iter)

    @work(thread=True)
    def _stream_agent_worker(self, header: str, events_iter: Iterator[AgentEvent | str]) -> None:
        text_area = self.query_one(TextArea)
        answer = ""
        last_update = 0.0
        tool_lines: list[str] = []

        def flush() -> None:
            text_area.clear()
            lines = [header, ""]
            if tool_lines:
                lines.extend(tool_lines)
                lines.append("")
            lines.append(answer or "Thinking...")
            text_area.write("\n".join(lines))

        try:
            for event in events_iter:
                if isinstance(event, str):
                    answer += event
                elif event.kind == "tool":
                    tool_lines.append(self._format_tool_line(event))
                elif event.kind == "text":
                    answer += event.content

                now = time.monotonic()
                if now - last_update >= 0.1 or (not answer and tool_lines):
                    self.call_from_thread(flush)
                    last_update = now
            answer = answer.strip()
            suggestions = generate_suggestions(answer) if answer else []
        except Exception as exc:
            self.call_from_thread(
                self._render_agent_error,
                header,
                f"Agent error: {exc}",
            )
            return

        self.call_from_thread(
            self._render_agent_result,
            header,
            answer,
            suggestions,
            tool_lines,
        )

    def _render_agent_error(self, header: str, message: str) -> None:
        self.query_one(SuggestionBar).clear_suggestions()
        self.query_one(TextArea).clear()
        self.query_one(TextArea).write(f"{header}\n\n{message}")

    def _answer_agent_question(self, query: str, context: str) -> None:
        header = f"Question: {query}"
        history = self.query_one(TerminalView).history_snapshot(limit=50)
        self._stream_agent_response(header, ask(query, context=context, history=history))

    def _explain_agent_command(self, command: str, context: str) -> None:
        header = f"Explanation: {command}"
        history = self.query_one(TerminalView).history_snapshot(limit=50)
        self._stream_agent_response(header, ask_command(command, context=context, history=history))

    def _explain_agent_error(self, command: str, error_info: str) -> None:
        header = f"Error Help: {command}"
        self._stream_agent_response(header, ask_error(error_info))

    # TODO: TOOL: web search for documentation

def main() -> None:
    app = TerminalApp()
    app.run()

if __name__ == "__main__":
    main()