' PsygaBio Production System - hidden background launcher (Windows).
' Double-click this file to start the app with NO console window.
' The server keeps running detached; open http://127.0.0.1:5000
' (or :5001 with PORT set - see run_hidden.bat).
' To stop it, end the python.exe process (Task Manager) or reboot.
Option Explicit
Dim sh, dir
Set sh = CreateObject("WScript.Shell")
dir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
' 0 = hidden window, False = do not wait
sh.Run """" & dir & "\run_hidden.bat""", 0, False
