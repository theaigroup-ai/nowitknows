@echo off
rem Windows cannot start nowitknows.sh, so this runs it with Git Bash, looked for where Claude Code looks.
setlocal
set "nik_self=%~dp0"
set "nik_self=%nik_self:\=/%"
set "nik_bash=%CLAUDE_CODE_GIT_BASH_PATH%"
if defined nik_bash if exist "%nik_bash%" goto run
set "nik_bash=%ProgramFiles%\Git\bin\bash.exe"
if exist "%nik_bash%" goto run
set "nik_bash=%LOCALAPPDATA%\Programs\Git\bin\bash.exe"
if exist "%nik_bash%" goto run
for /f "delims=" %%g in ('where git 2^>nul') do if exist "%%~dpg..\bin\bash.exe" (
  set "nik_bash=%%~dpg..\bin\bash.exe"
  goto run
)
echo nowitknows needs Git Bash, and none was found. Install Git for Windows for your user, then start a new session. 1>&2
exit /b 1
:run
"%nik_bash%" "%nik_self%nowitknows.sh" %*
exit /b %errorlevel%
