"""Shared desktop palette and modal-dialog lifecycle."""
import tkinter as tk
from tkinter import ttk

BG, PANEL, INK, MUTED, ACCENT = "#11151d", "#1b2230", "#e5ebf6", "#93a2b9", "#58d5bc"


class Dialog(tk.Toplevel):
    def __init__(self, app, title, width=620):
        super().__init__(app.root)
        self.app = app
        self.title(title)
        self.configure(bg=PANEL)
        self.resizable(False, False)
        self.transient(app.root)
        self.body = ttk.Frame(self, padding=25, style="Panel.TFrame")
        self.body.pack(fill="both", expand=True)
        self.body.columnconfigure(0, weight=1)
        self.geometry(f"{width}x400+{app.root.winfo_x()+100}+{app.root.winfo_y()+40}")
        self.grab_set()
        self.focus_timer = self.after_idle(self.focus_set)
        self.bind("<Escape>", lambda e: self.destroy())

    def label(self, text, row):
        ttk.Label(self.body, text=text, style="Muted.TLabel").grid(row=row, column=0, sticky="w", pady=(10, 5))

    def destroy(self):
        for name in ("focus_timer", "entry_focus_timer", "update_timer"):
            timer = getattr(self, name, None)
            if timer:
                self.after_cancel(timer)
        if getattr(self, "cancel", None):
            self.cancel.set()
        super().destroy()
        # Widget/master cycles can be collected by a later network worker.
        # Release this dialog's Tcl variables now, while on the UI thread.
        for key, value in list(vars(self).items()):
            if isinstance(value, tk.Variable):
                delattr(self, key)
            elif isinstance(value, dict):
                for variable_key, variable in list(value.items()):
                    if isinstance(variable, tk.Variable):
                        del value[variable_key]
