# Установка Python для Universal RenPy Translator / Python installer for Universal RenPy Translator
# Файл должен быть сохранён в UTF-8 с BOM (так PowerShell 5 правильно читает русский текст).
# Коды выхода: 0 - Python установлен, 2 - пользователь отказался, 1 - ошибка.
$ErrorActionPreference = 'Stop'

# Версии Python (первая доступная на python.org будет скачана). При необходимости поправьте список.
$Candidates = @('3.14.7', '3.14.6', '3.14.5', '3.14.4', '3.14.3', '3.14.2', '3.14.1', '3.14.0')

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

$Lang = (Get-UICulture).TwoLetterISOLanguageName
# T: текст на языке интерфейса системы (ru / en / zh / es / pt; для остальных языков - английский)
function T([string]$ru, [string]$en, [string]$zh, [string]$es, [string]$pt) {
    switch ($Lang) {
        'ru' { return $ru }
        'zh' { if ($zh) { return $zh } else { return $en } }
        'es' { if ($es) { return $es } else { return $en } }
        'pt' { if ($pt) { return $pt } else { return $en } }
        default { return $en }
    }
}
function Show-Box([string]$text, [string]$buttons, [string]$icon) {
    return [System.Windows.Forms.MessageBox]::Show($text, 'Universal RenPy Translator', $buttons, $icon)
}

$Arch = 'amd64'
if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { $Arch = 'arm64' }

# --- 1. Ищем доступную версию -------------------------------------------------
$Version = $null
foreach ($v in $Candidates) {
    $u = "https://www.python.org/ftp/python/$v/python-$v-$Arch.exe"
    try {
        $r = Invoke-WebRequest -Uri $u -Method Head -UseBasicParsing -TimeoutSec 20
        if ($r.StatusCode -eq 200) { $Version = $v; $Url = $u; break }
    } catch {}
}
if (-not $Version) {
    Show-Box (T "Не удалось найти установщик Python на python.org. Проверьте подключение к интернету или установите Python вручную: https://www.python.org/downloads/" "Could not find a Python installer on python.org. Check your internet connection or install Python manually: https://www.python.org/downloads/" "在 python.org 上找不到 Python 安装程序。请检查网络连接，或手动安装 Python：https://www.python.org/downloads/" "No se pudo encontrar un instalador de Python en python.org. Compruebe su conexión a internet o instale Python manualmente: https://www.python.org/downloads/" "Não foi possível encontrar um instalador do Python em python.org. Verifique sua conexão com a internet ou instale o Python manualmente: https://www.python.org/downloads/") 'OK' 'Error' | Out-Null
    exit 1
}

# --- 2. Спрашиваем разрешение -------------------------------------------------
$q = T "Python (с модулем tkinter) не найден на этом компьютере, а он нужен для работы программы.`n`nСкачать и установить Python $Version автоматически?`n`nЗагрузка около 30 МБ. Установка - только для текущего пользователя, права администратора не нужны." "Python (with tkinter) was not found on this computer, but the program needs it.`n`nDownload and install Python $Version automatically?`n`nAbout 30 MB to download. Installed for the current user only, no administrator rights needed." "此电脑上未找到 Python（含 tkinter），而程序需要它。`n`n是否自动下载并安装 Python ${Version}？`n`n下载大小约 30 MB。仅为当前用户安装，不需要管理员权限。" "No se encontró Python (con tkinter) en este equipo, pero el programa lo necesita.`n`n¿Descargar e instalar Python ${Version} automáticamente?`n`nUnos 30 MB de descarga. Se instala solo para el usuario actual, no hacen falta derechos de administrador." "O Python (com tkinter) não foi encontrado neste computador, mas o programa precisa dele.`n`nBaixar e instalar o Python ${Version} automaticamente?`n`nCerca de 30 MB para baixar. Instalado apenas para o usuário atual, não são necessários direitos de administrador."
$ans = Show-Box $q 'YesNo' 'Question'
if ($ans -ne 'Yes') { exit 2 }

# --- 3. Окно прогресса ----------------------------------------------------------
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Universal RenPy Translator'
$form.Size = New-Object System.Drawing.Size(480, 150)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.ControlBox = $false
$label = New-Object System.Windows.Forms.Label
$label.Location = New-Object System.Drawing.Point(15, 15)
$label.Size = New-Object System.Drawing.Size(440, 40)
$bar = New-Object System.Windows.Forms.ProgressBar
$bar.Location = New-Object System.Drawing.Point(15, 65)
$bar.Size = New-Object System.Drawing.Size(440, 24)
$bar.Minimum = 0
$bar.Maximum = 100
$form.Controls.Add($label)
$form.Controls.Add($bar)
$form.Show()
[System.Windows.Forms.Application]::DoEvents()

$installer = Join-Path $env:TEMP "python-$Version-$Arch.exe"
try {
    # --- 4. Скачивание с отображением прогресса ---------------------------------
    $label.Text = (T "Скачиваю Python $Version ..." "Downloading Python $Version ..." "正在下载 Python ${Version} ……" "Descargando Python ${Version} ..." "Baixando o Python ${Version} ...")
    $req = [System.Net.HttpWebRequest]::Create($Url)
    $req.UserAgent = 'UniversalRenPyTranslator'
    $resp = $req.GetResponse()
    $total = $resp.ContentLength
    $stream = $resp.GetResponseStream()
    $fs = [System.IO.File]::Create($installer)
    $buf = New-Object byte[] 262144
    $done = 0
    while (($n = $stream.Read($buf, 0, $buf.Length)) -gt 0) {
        $fs.Write($buf, 0, $n)
        $done += $n
        if ($total -gt 0) {
            $bar.Value = [int](100 * $done / $total)
            $label.Text = (T "Скачиваю Python $Version : " "Downloading Python $Version : " "正在下载 Python ${Version} ：" "Descargando Python ${Version} : " "Baixando o Python ${Version} : ") + ('{0:N1} / {1:N1} MB' -f ($done / 1MB), ($total / 1MB))
        }
        [System.Windows.Forms.Application]::DoEvents()
    }
    $fs.Close(); $stream.Close(); $resp.Close()

    # --- 5. Установка (у установщика есть собственное окно прогресса) -------------
    $label.Text = (T "Устанавливаю Python $Version ... Это займёт около минуты." "Installing Python $Version ... This takes about a minute." "正在安装 Python ${Version} ……大约需要一分钟。" "Instalando Python ${Version} ... Tarda aproximadamente un minuto." "Instalando o Python ${Version} ... Leva cerca de um minuto.")
    $bar.Style = 'Marquee'
    $installArgs = @('/passive', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_pip=1', 'Include_tcltk=1', 'Include_test=0', 'Include_doc=0')
    $p = Start-Process -FilePath $installer -ArgumentList $installArgs -PassThru
    while (-not $p.HasExited) { Start-Sleep -Milliseconds 200; [System.Windows.Forms.Application]::DoEvents() }
    $form.Close()
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { throw "installer exit code $($p.ExitCode)" }
    Show-Box (T "Python $Version установлен. Сейчас запустится программа." "Python $Version has been installed. The program will start now." "Python ${Version} 已安装。程序即将启动。" "Python ${Version} se ha instalado. El programa se iniciará ahora." "O Python ${Version} foi instalado. O programa será iniciado agora.") 'OK' 'Information' | Out-Null
    exit 0
} catch {
    try { $form.Close() } catch {}
    Show-Box ((T "Не удалось установить Python: " "Could not install Python: " "无法安装 Python：" "No se pudo instalar Python: " "Não foi possível instalar o Python: ") + $_.Exception.Message) 'OK' 'Error' | Out-Null
    exit 1
} finally {
    if (Test-Path $installer) { Remove-Item $installer -Force -ErrorAction SilentlyContinue }
}
