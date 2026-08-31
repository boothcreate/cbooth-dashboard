' run_hidden.vbs — starts the dashboard server with no console window.
' Launched at logon via a shortcut in the Windows Startup folder (see
' BUILD.md, "Running always-on"). Not meant to be double-clicked for normal
' interactive use — for that, use run.bat instead, which shows the window
' and opens the browser.

Set WshShell = CreateObject("WScript.Shell")
pythonw = """C:\Users\smutl\dashboard\.venv\Scripts\pythonw.exe"""
serveScript = """C:\Users\smutl\dashboard\scripts\serve.py"""
WshShell.CurrentDirectory = "C:\Users\smutl\dashboard"
WshShell.Run pythonw & " " & serveScript, 0, False
