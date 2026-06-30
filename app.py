import os
import sys
from pathlib import Path


try:
    import gi
    gi.require_version('Gtk', '3.0')
    gi.require_version('Vte', '2.91')
    from gi.repository import Gtk, Vte, GLib, Gdk
except ImportError as e:
    print(f"\n[!] ERRORE: Mancano le librerie di sistema per l'interfaccia nativa. Dettagli: {e}")

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

        # Non forziamo la maschera SHIFT su ogni evento mouse: questo impedirebbe
        # a Textual (in esecuzione dentro VTE) di ricevere gli eventi mouse,
        # rompendo il drag della ResizeHandle del BottomPanel.
        # Per la selezione testuale nativa l'utente tiene premuto SHIFT
        # (comportamento standard di VTE).

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

        # Avviamo il processo python (cli.py) all'interno del widget terminale.
        # Quando l'app è distribuita con PyInstaller, sys.executable punta al
        # binario frozen (lterm) e non a un interprete Python: in quel caso
        # lanciamo il companion executable "lterm-cli" generato dal build script.
        if hasattr(sys, "_MEIPASS"):
            cli_bin = Path(sys._MEIPASS) / "lterm-cli"
            if not cli_bin.exists():
                print(f"Errore: companion executable non trovato: {cli_bin}")
                sys.exit(1)
            cmd = [str(cli_bin)]
        else:
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
    # Risoluzione del percorso di cli.py.
    # In un build PyInstaller --onefile i file sono estratti in una cartella
    # temporanea esposta tramite sys._MEIPASS; in sviluppo si usa la cartella
    # accanto a questo script.
    if hasattr(sys, "_MEIPASS"):
        app_dir = Path(sys._MEIPASS)
    else:
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