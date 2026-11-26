$ErrorActionPreference="Stop"
$runtime=Join-Path $PSScriptRoot 'fab_shelf\runtime'
$installer=Join-Path $runtime 'MicrosoftEdgeWebview2Setup.exe'
$runtimeModule=Get-Content (Join-Path $PSScriptRoot 'fab_shelf\webview_runtime.py') -Raw
if($runtimeModule -notmatch 'BOOTSTRAPPER_SHA256 = "([0-9a-f]{64})"' -or (Get-FileHash $installer).Hash -ne $Matches[1]){
    throw "WebView2 bootstrapper is missing or does not match the verified Microsoft binary"
}
$csc=Join-Path $env:windir 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$profileSource=Join-Path $PSScriptRoot 'FabProfile.cs'
$ipcSource=Join-Path $PSScriptRoot 'IpcWakeup.cs'
& $csc /nologo /target:winexe /platform:x64 "/win32icon:$runtime\app.ico" "/out:$runtime\ShelfAuth093.exe" "/r:$runtime\Microsoft.Web.WebView2.Core.dll" "/r:$runtime\Microsoft.Web.WebView2.WinForms.dll" /r:System.Web.Extensions.dll (Join-Path $PSScriptRoot 'Sync.cs') $profileSource $ipcSource
if($LASTEXITCODE){throw "Sync build failed"}
& $csc /nologo /target:winexe /platform:x64 "/win32icon:$runtime\app.ico" "/out:$runtime\Gallery091.exe" "/r:$runtime\Microsoft.Web.WebView2.Core.dll" "/r:$runtime\Microsoft.Web.WebView2.WinForms.dll" /r:System.Web.Extensions.dll (Join-Path $PSScriptRoot 'Gallery.cs') (Join-Path $PSScriptRoot 'FabBrowser.cs') $profileSource $ipcSource
if($LASTEXITCODE){throw "Gallery build failed"}
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$out=Join-Path $PSScriptRoot 'FabShelf-Blender-Windows.zip'
$stream=[IO.File]::Create($out)
try{
    $zip=[IO.Compression.ZipArchive]::new($stream,'Create')
    try{
        $base=Join-Path $PSScriptRoot 'fab_shelf'
        foreach($name in @('LICENSE','COPYING.md')){
            [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip,(Join-Path $PSScriptRoot $name),"fab_shelf/$name")
        }
        foreach($file in Get-ChildItem $base -File -Recurse){
            if($file.Directory.Name -eq '__pycache__'){continue}
            if($file.Extension -eq '.exe' -and $file.Name -notin @('Gallery091.exe','ShelfAuth093.exe','MicrosoftEdgeWebview2Setup.exe')){continue}
            if($file.FullName -eq (Join-Path $base 'native.py')){continue}
            $entry='fab_shelf/'+$file.FullName.Substring($base.Length+1).Replace('\','/')
            [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip,$file.FullName,$entry)
        }
    }finally{$zip.Dispose()}
}finally{$stream.Dispose()}













