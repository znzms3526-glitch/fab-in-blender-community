param([ValidateSet('Absent','Present','Report')][string]$Expect='Report')
$ErrorActionPreference='Stop'
$client='SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
$found=$false
foreach($scope in @('CurrentUser','LocalMachine')){
    $view=if($scope -eq 'LocalMachine'){'Registry32'}else{'Default'}
    $root=[Microsoft.Win32.RegistryKey]::OpenBaseKey($scope,$view)
    try{
        $key=$root.OpenSubKey($client)
        if($key){
            try{
                $value=$key.GetValue('pv')
                if($value -and [version]$value -gt [version]'0.0.0.0'){
                    Write-Output "$scope WebView2 $value"
                    $found=$true
                }
            }finally{$key.Dispose()}
        }
    }finally{$root.Dispose()}
}
if(-not $found){Write-Output 'No registered Evergreen runtime.'}
if(($Expect -eq 'Absent' -and $found) -or ($Expect -eq 'Present' -and -not $found)){
    throw "WebView2 registration did not match expected state: $Expect"
}
