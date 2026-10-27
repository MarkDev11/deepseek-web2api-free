@echo off
rem Buka helper untuk tombol "Browser login" di WebUI Vercel.
rem Jendela ini harus tetap terbuka selama menambah akun.
node "%~dp0dsh-plugin\scripts\login-helper.mjs"
pause
