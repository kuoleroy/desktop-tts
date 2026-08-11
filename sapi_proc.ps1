param([string]$InPath, [string]$OutPath, [string]$Voice, [int]$Rate)
$s = New-Object -ComObject SAPI.SpVoice
$v = $null
foreach ($vv in $s.GetVoices()) {
    if ($vv.GetDescription() -like "*$Voice*") { $v = $vv; break }
}
if (-not $v) {
    foreach ($vv in $s.GetVoices()) {
        $d = $vv.GetDescription()
        if ($d -match 'Chinese|Huihui|Kangkang') { $v = $vv; break }
    }
}
if (-not $v) {
    Write-Error "no chinese voice installed"
    exit 1
}
$s.Voice = $v
try { $s.Rate = $Rate } catch { }
$fs = New-Object -ComObject SAPI.SpFileStream
$fs.Open($OutPath, 3)
$s.AudioOutputStream = $fs
$s.Speak((Get-Content -Path $InPath -Raw -Encoding UTF8))
$fs.Close()
