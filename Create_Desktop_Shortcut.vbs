Set WshShell = CreateObject("WScript.Shell")
strDesktop = WshShell.SpecialFolders("Desktop")
strCurrentDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
Set oShellLink = WshShell.CreateShortcut(strDesktop & "\SPAGASUS JARVIS.lnk")
oShellLink.TargetPath = strCurrentDir & "\SPAGASUS JARVIS.exe"
oShellLink.WorkingDirectory = strCurrentDir
oShellLink.WindowStyle = 1
oShellLink.IconLocation = strCurrentDir & "\spagasus.ico"
oShellLink.Description = "SPAGASUS JARVIS AI Assistant"
oShellLink.Save
MsgBox "Desktop shortcut created successfully!", 64, "SPAGASUS JARVIS"
