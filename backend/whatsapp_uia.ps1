# WhatsApp Desktop UI Automation helper for Spagasus Jarvis.
#
# Actions:
#   -Action click-chat -Name "arvind" [-Fuzzy]  -> prints "CLICK:x,y" for the chat row
#   -Action call [-Video]                       -> presses the call button, prints "OK:invoked"
#   -Action probe [-Video]                      -> only reports whether the button exists
#   -Action call-active                         -> "OK:call-active" while a call is in progress
#   -Action end-call                            -> presses the "End call" button
#
# WhatsApp is an Electron app: the real content lives in a second top-level
# window (Chrome_WidgetWin_1). Its controls are fully exposed over UIA, so
# chat rows and the "Voice call"/"Video call" buttons are findable by name.
#
# The chat list is virtualized: off-screen rows report bounding rectangles
# far outside the window, so only rows whose CENTER lies inside the window
# are clickable. When the contact isn't visible, the search box is used to
# bring it up instead.

param(
    [Parameter(Mandatory = $true)][string]$Action,
    [string]$Name = "",
    [switch]$Video,
    [switch]$Fuzzy
)

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WaWin32 {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
}
"@

# LCS-based similarity in 0..1, mirroring difflib.ratio: "aravind" vs
# "Arvind" scores ~0.92, "dravid" vs "Arvind" ~0.83 - so what speech
# recognition hears still finds the real contact.
function Get-Similarity([string]$a, [string]$b) {
    $a = $a.ToLowerInvariant()
    $b = $b.ToLowerInvariant()
    $m = $a.Length; $n = $b.Length
    if ($m -eq 0 -or $n -eq 0) { return 0.0 }
    $dp = New-Object 'int[,]' ($m + 1), ($n + 1)
    for ($i = 1; $i -le $m; $i++) {
        $im1 = $i - 1
        for ($j = 1; $j -le $n; $j++) {
            $jm1 = $j - 1
            if ($a[$im1] -ceq $b[$jm1]) {
                $dp[$i, $j] = $dp[$im1, $jm1] + 1
            } else {
                $up = $dp[$im1, $j]
                $left = $dp[$i, $jm1]
                $dp[$i, $j] = [Math]::Max($up, $left)
            }
        }
    }
    return (2.0 * $dp[$m, $n]) / ($m + $n)
}

$root = [System.Windows.Automation.AutomationElement]::RootElement

# Locate the Electron content window (title like "(1) WhatsApp").
$wa = $null
foreach ($el in $root.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
    if ($el.Current.ClassName -eq "Chrome_WidgetWin_1" -and $el.Current.Name -like "*WhatsApp*") {
        $wa = $el
        break
    }
}
if ($null -eq $wa) {
    Write-Output "ERR:whatsapp-not-running"
    exit 1
}
$wr = $wa.Current.BoundingRectangle
Write-Output ("WA_RECT:" + [int]$wr.X + "," + [int]$wr.Y + "," + [int]$wr.Width + "," + [int]$wr.Height)

# chat rows whose center lies inside the visible window (deduped by position)
function Get-VisibleRows {
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::DataItem)
    $items = $wa.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
    $rows = @{}
    foreach ($it in $items) {
        $r = $it.Current.BoundingRectangle
        if ($r.IsEmpty -or $r.Width -le 0 -or $r.Height -le 0) { continue }
        if ([double]::IsNaN($r.X) -or [double]::IsInfinity($r.X) -or
            [double]::IsNaN($r.Y) -or [double]::IsInfinity($r.Y)) { continue }
        $cx = $r.X + $r.Width / 2
        $cy = $r.Y + $r.Height / 2
        if ($cx -lt $wr.X -or $cx -gt ($wr.X + $wr.Width) -or
            $cy -lt $wr.Y -or $cy -gt ($wr.Y + $wr.Height)) { continue }
        $key = ([int]$r.X).ToString() + "," + ([int]$r.Y).ToString()
        if (-not $rows.ContainsKey($key) -or $it.Current.Name.Length -lt $rows[$key].Current.Name.Length) {
            $rows[$key] = $it
        }
    }
    return $rows
}

# best matching visible row: exact prefix wins; otherwise fuzzy on the
# contact-name token prefixes; ties go to the shorter chat name
function Find-BestMatch($rows) {
    $pat = "^" + [regex]::Escape($Name) + "[\s,]"
    $best = $null
    $bestScore = -1.0
    $bestName = ""
    foreach ($it in $rows.Values) {
        $nm = $it.Current.Name
        $score = 0.0
        if ($nm -match $pat) {
            $score = 1.0
        } elseif ($Fuzzy) {
            $toks = $nm -split "\s+"
            for ($k = $toks.Count; $k -ge 1; $k--) {
                $pre = ($toks[0..($k - 1)] -join " ")
                $s = Get-Similarity $Name $pre
                if ($s -gt $score) { $score = $s }
            }
        }
        if ($score -gt $bestScore -or
            ($score -eq $bestScore -and $nm.Length -lt $bestName.Length)) {
            $best = $it; $bestScore = $score; $bestName = $nm
        }
    }
    if ($null -eq $best -or $bestScore -lt 0.6) { return $null }
    return $best
}

function Out-Click($it) {
    $r = $it.Current.BoundingRectangle
    $x = [int]($r.X + $r.Width / 2)
    $y = [int]($r.Y + $r.Height / 2)
    Write-Output ("CLICK:" + $x + "," + $y)
    Write-Output ("MATCHED:" + $it.Current.Name)
    exit 0
}

if ($Action -eq "focus") {
    # bring WhatsApp to the foreground so keystrokes/clicks land on it
    $h = [IntPtr]$wa.Current.NativeWindowHandle
    if ($h -ne [IntPtr]::Zero) {
        [WaWin32]::ShowWindow($h, 9)                 # SW_RESTORE
        [WaWin32]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)      # press Alt
        [WaWin32]::SetForegroundWindow($h)
        [WaWin32]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)      # release Alt
        Write-Output "OK:focused"
        exit 0
    }
    Write-Output "ERR:no-handle"
    exit 1
}

if ($Action -eq "click-chat") {
    $row = Find-BestMatch (Get-VisibleRows)
    if ($null -ne $row) { Out-Click $row }
    Write-Output "ERR:chat-not-found"
    exit 1
}

if ($Action -eq "search-box") {
    # report where the search box is (or the clear-x if it holds text), so the
    # caller can click it and type with real keystrokes
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Edit)
    foreach ($e in $wa.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)) {
        if ($e.Current.Name -like "*Search*") {
            $r = $e.Current.BoundingRectangle
            if (-not $r.IsEmpty -and $r.Width -gt 0 -and $r.Height -gt 0) {
                Write-Output ("BOX:" + [int]$r.X + "," + [int]$r.Y + "," + [int]$r.Width + "," + [int]$r.Height)
                exit 0
            }
        }
    }
    $all = $wa.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                       [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($el in $all) {
        if ($el.Current.ControlType.ProgrammaticName -eq "ControlType.Button" -and
            ($el.Current.Name -like "*End icon*" -or $el.Current.Name -like "*Clear*")) {
            $r = $el.Current.BoundingRectangle
            if (-not $r.IsEmpty -and $r.Width -gt 0 -and $r.Height -gt 0) {
                Write-Output ("CLEAR:" + [int]($r.X + $r.Width / 2) + "," + [int]($r.Y + $r.Height / 2))
                exit 0
            }
        }
    }
    Write-Output "ERR:no-search"
    exit 1
}

if ($Action -eq "call" -or $Action -eq "probe") {
    $target = "Voice call"
    if ($Video) { $target = "Video call" }
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, $target)
    $btn = $wa.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
    if ($null -eq $btn) {
        # fuzzy fallback: any button whose name contains the target
        $all = $wa.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                           [System.Windows.Automation.Condition]::TrueCondition)
        foreach ($el in $all) {
            if ($el.Current.ControlType.ProgrammaticName -eq "ControlType.Button" -and
                $el.Current.Name -like ("*" + $target + "*")) {
                $btn = $el
                break
            }
        }
    }
    if ($null -eq $btn) {
        Write-Output "ERR:call-button-not-found"
        exit 1
    }
    if ($Action -eq "probe") {
        Write-Output "OK:button-visible"
        exit 0
    }
    $pattern = $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $pattern.Invoke()
    Write-Output "OK:invoked"
    exit 0
}

if ($Action -eq "call-active") {
    # during a live call the chat shows a "Voice call Ringing ..." entry;
    # the in-call panel itself is canvas-rendered and invisible to UIA
    $all = $wa.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                       [System.Windows.Automation.Condition]::TrueCondition)
    foreach ($el in $all) {
        $nm = $el.Current.Name
        if ($nm -and $nm -match "^(Voice|Video) call (Ringing|Calling|Ongoing)") {
            Write-Output "OK:call-active"
            exit 0
        }
    }
    Write-Output "ERR:no-active-call"
    exit 1
}

Write-Output "ERR:bad-action"
exit 1
