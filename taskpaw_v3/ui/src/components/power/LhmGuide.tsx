// #345: the LibreHardwareMonitor install guide under a sensor line without LHM (approved preview
// power-sensors-preview.html). The commands are String.raw so their backslashes stay literal.
import { useEffect, useRef, useState } from "react";

export const INSTALL_CMD = String.raw`winget install --id LibreHardwareMonitor.LibreHardwareMonitor -e --accept-source-agreements`;

export const SETUP_CMD = String.raw`$exe = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages","$env:ProgramFiles\WinGet\Packages","$env:ProgramFiles\LibreHardwareMonitor" -Recurse -Filter LibreHardwareMonitor.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
if (-not $exe) { throw "LibreHardwareMonitor.exe not found" }
$dir = Split-Path $exe; $cfg = Join-Path $dir "LibreHardwareMonitor.config"
@'
<?xml version="1.0" encoding="utf-8"?>
<configuration><appSettings>
<add key="runWebServerMenuItem" value="true" /><add key="listenerPort" value="8085" /><add key="listenerIp" value="127.0.0.1" />
<add key="startMinMenuItem" value="true" /><add key="minTrayMenuItem" value="true" /><add key="minCloseMenuItem" value="true" />
</appSettings></configuration>
'@ | Set-Content -Path $cfg -Encoding UTF8
Register-ScheduledTask -TaskName "LibreHardwareMonitor" -Action (New-ScheduledTaskAction -Execute $exe -WorkingDirectory $dir) -Trigger (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME") -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)) -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName "LibreHardwareMonitor"; Start-Sleep 15
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8085/metrics).Content | Select-String 'temperature_celsius|fan_rpm'`;

const COPIED_MS = 1500;

function selectText(el: HTMLElement) {
  const range = document.createRange();
  range.selectNodeContents(el);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
}

// The clipboard call stays inside the click handler (user gesture); a refusal or a missing API selects the text.
function Cmd({ label, text }: { label: string; text: string }) {
  const pre = useRef<HTMLPreElement>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), COPIED_MS);
    return () => clearTimeout(t);
  }, [copied]);
  const select = () => { if (pre.current) selectText(pre.current); };
  const copy = () => {
    if (!navigator.clipboard?.writeText) { select(); return; }
    navigator.clipboard.writeText(text).then(() => setCopied(true), select);
  };
  return (
    <div className="cmd">
      <div className="cmd-head"><span>{label}</span><button type="button" className="copy" onClick={copy}>{copied ? "已复制" : "复制"}</button></div>
      <pre ref={pre}>{text}</pre>
    </div>
  );
}

export function LhmGuide({ machine }: { machine: string }) {
  return (
    <div className="m-guide">
      <p className="lead">在 {machine} 上装一次 LibreHardwareMonitor，这一行就会显示 CPU 温度和风扇转速。</p>
      <ol>
        <li>在 <b>{machine}</b> 上打开<b>管理员</b> PowerShell，先跑第 1 段安装，再跑第 2 段设置。</li>
        <li>第一次启动若弹出安装 PawnIO 驱动的提示，点同意。</li>
        <li>它在 8085 端口提供只读的传感器读数（局域网内可见），TaskPaw 无需任何配置，Agent 下次采样即显示。</li>
      </ol>
      <Cmd label="1 · 安装" text={INSTALL_CMD} />
      <Cmd label="2 · 设置并启动（写配置、开机自启、立即启动、打印读数验证）" text={SETUP_CMD} />
      <p className="note">只需做一次；TaskPaw 不会自动安装或远程推送。复制按钮不可用时，直接选中命令文本复制。</p>
    </div>
  );
}
