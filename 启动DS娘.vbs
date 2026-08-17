' Double-click to start DS Assistant without a console window.
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir

pythonw = ""
If fso.FileExists(scriptDir & "\venv\Scripts\pythonw.exe") Then
    pythonw = scriptDir & "\venv\Scripts\pythonw.exe"
ElseIf fso.FileExists(scriptDir & "\venv\Scripts\python.exe") Then
    pythonw = scriptDir & "\venv\Scripts\python.exe"
End If

If pythonw = "" Then
    shell.Run "cmd /c python run.py", 1, False
Else
    shell.Run """" & pythonw & """ """ & scriptDir & "\run.py""", 0, False
End If
