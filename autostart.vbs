' Launch screen-dimmer-win without a console window.
' Change SHOW_WINDOW to 0 if you want the app window hidden as well.
Const SHOW_WINDOW = 1

Set fso = CreateObject("Scripting.FileSystemObject")
Set ws = CreateObject("WScript.Shell")

' Resolve paths relative to this .vbs file, wherever it lives.
base = fso.GetParentFolderName(WScript.ScriptFullName)
ws.CurrentDirectory = base

pythonw = base & "\.venv\Scripts\pythonw.exe"
app = base & "\screen-dimmer-win.py"

cmd = """" & pythonw & """ """ & app & """"
ws.Run cmd, SHOW_WINDOW, False
