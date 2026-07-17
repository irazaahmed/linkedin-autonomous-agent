Add-Type -AssemblyName System.Windows.Forms
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Information
$notify.Visible = $true
$notify.BalloonTipTitle = "LinkedIn Commenter"
$notify.BalloonTipText = "Reminder: double-click the 'LinkedIn Auto-Commenter' shortcut on your Desktop to run today's session."
$notify.ShowBalloonTip(15000)
Start-Sleep -Seconds 16
$notify.Dispose()
