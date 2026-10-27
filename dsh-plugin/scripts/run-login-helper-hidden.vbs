' Start the dsweb login helper with NO console window.
' Registered as the "dsweb-login-helper" scheduled task (runs at logon).
Set sh = CreateObject("WScript.Shell")
sh.Run """C:\Program Files\nodejs\node.exe"" ""E:\TESTING\deepseekapi\dsh-plugin\scripts\login-helper.mjs""", 0, False
