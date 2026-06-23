import os

from textual.app import App, ComposeResult

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

    def compose(self) -> ComposeResult:
        yield StatusHeader()
        yield TerminalView()
        yield BottomPanel(id="bottom-dock")

    def on_mount(self) -> None:
        self.query_one(TextArea).write("Welcome to LTerm! I'm your terminal learning assistant.")
        self.query_one(SuggestionBar).add_suggestion("Try typing 'help' to see available commands.")

    def on_suggestion_pressed(self, event: SuggestionBar.Pressed) -> None:
        terminal = self.query_one(TerminalView)
        if terminal._fd is not None and terminal._running:
            try:
                os.write(terminal._fd, event.text.encode())
            except OSError:
                pass

    def on_shell_command_executed(self, event: TerminalView.CommandExecuted) -> None:
        ok = event.exit_code == 0
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
            self.query_one(TextArea).clear()
            self.query_one(TextArea).write(error_info)

    ## Agent

    # TODO: on error agent print explaination and suggestions for next steps

    # TODO: on -- question, agent answers question and prints suggestions for next steps

    # TODO: on ?? command, agent explains command and prints suggestions for next steps

    # TODO: on suggestion pressed thea agent explains the command

    # TODO: agent must have function calling capabilities

    # TODO: TOOL: retrieve history and check for last intents

    # TODO: TOOL: retrieve mandb documentation for command

    # TODO: TOOL: web search for documentation

    # TODO: Config file for llm config

def main() -> None:
    app = TerminalApp()
    app.run()

if __name__ == "__main__":
    main()