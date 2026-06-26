import os
import sys
from pathlib import Path

# Tentativo di importare le librerie GTK, VTE e GDK
try:
    import gi
    gi.require_version('Gtk', '3.0')
    gi.require_version('Vte', '2.91')
    from gi.repository import Gtk, Vte, GLib, Gdk
except ImportError as e:
    print(f"\n[!] ERRORE: Mancano le librerie di sistema per l'interfaccia nativa. Dettagli: {e}")
    print("Per installarle su Ubuntu/Debian, esegui questo comando:")
    print("sudo apt update && sudo apt install python3-gi gir1.2-vte-2.91")
    sys.exit(1)

class LTermNativeApp(Gtk.Window):
    def __init__(self, command_path: str):
        # Configurazione finestra principale
        super().__init__(title="LTerm - Desktop Native")
        self.set_default_size(1100, 750)
        self.set_border_width(0)

        # Inizializzazione del terminale
        self.terminal = Vte.Terminal()

        # Configurazione Copia/Incolla e scorciatoie da tastiera
        self.terminal.connect("key-press-event", self._on_key_press)

        # Forza la selezione testuale nativa (bypassando la cattura del mouse di Textual)
        self.terminal.connect("button-press-event", self._force_native_selection)
        self.terminal.connect("motion-notify-event", self._force_native_selection)
        self.terminal.connect("button-release-event", self._force_native_selection)

        # --- Gestione robusta del cursore ---
        try:
            if hasattr(self.terminal, 'set_cursor_blink_mode'):
                try:
                    self.terminal.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
                except:
                    self.terminal.set_cursor_blink_mode(True)
        except Exception:
            pass
        
        # Aggiungiamo il terminale alla finestra GTK
        self.add(self.terminal)

        # Avviamo il processo python (cli.py) all'interno del widget terminale
        cmd = [sys.executable, command_path]
        child_env = {**os.environ, "LTERM_NATIVE": "1"}
        envv = [f"{key}={value}" for key, value in child_env.items()]

        try:
            # Configurazione dei 9 argomenti per Vte.Terminal.spawn_sync
            self.terminal.spawn_sync(
                Vte.PtyFlags.DEFAULT,        # 1. pty_flags
                None,                        # 2. working_directory
                cmd,                         # 3. argv
                envv,                        # 4. envv
                GLib.SpawnFlags.DEFAULT,     # 5. spawn_flags
                None,                        # 6. child_setup
                None,                        # 7. child_setup_data
                None                         # 8. cancellable
            )
        except Exception as e:
            print(f"Errore durante lo spawn del processo: {e}")
            sys.exit(1)

        # Quando l'utente chiude la finestra, chiudiamo l'app correttamente
        self.connect("destroy", Gtk.main_quit)

    def _force_native_selection(self, widget, event):
        # Aggiunge la maschera SHIFT in modo che VTE esegua sempre la selezione
        # testuale del terminale e non inoltri l'evento all'app TUI (Textual)
        event.state |= Gdk.ModifierType.SHIFT_MASK
        return False

    def _on_key_press(self, widget, event):
        ctrl_shift = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
        if (event.state & ctrl_shift) == ctrl_shift:
            if event.keyval in (Gdk.KEY_C, Gdk.KEY_c):
                # Evita di svuotare la clipboard se non c'è nulla di selezionato
                if self.terminal.get_has_selection():
                    if hasattr(self.terminal, 'copy_clipboard_format'):
                        self.terminal.copy_clipboard_format(Vte.Format.TEXT)
                    else:
                        self.terminal.copy_clipboard()
                return True
            elif event.keyval in (Gdk.KEY_V, Gdk.KEY_v):
                self.terminal.paste_clipboard()
                return True
        return False

def main():
    # Otteniamo il percorso assoluto di cli.py
    app_dir = Path(__file__).resolve().parent
    cli_path = app_dir / "cli.py"

    if not cli_path.exists():
        print(f"Errore: Non è stato trovato il file {cli_path}")
        sys.exit(1)

    # Creazione e avvio dell'app
    app = LTermNativeApp(str(cli_path))
    app.show_all()
    Gtk.main()

if __name__ == "__main__":
    main()