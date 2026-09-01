<#
.SYNOPSIS
    Start llama.cpp's own HTTP server for the Friends Kitchen agents.

.DESCRIPTION
    `llama-server` is the fifth process in this system, beside the four agent
    services on 8100-8103. It loads one GGUF and serves it over an
    OpenAI-compatible /v1/chat/completions, which is what the `llamacpp`
    provider in agent/llm/providers.py talks to.

    Every setting it takes is read from .env, so this script and the running
    agents agree by construction rather than by somebody remembering to change
    two things:

        LLM_BASE_URL / LLAMACPP_BASE_URL   where to listen        (:8080)
        LLAMACPP_MODEL                     the -a alias the agents ask for
        LLAMACPP_GGUF                      which file to load
        LLAMACPP_CTX                       the context window, -c
        LLAMACPP_NGL                       layers on the GPU, -ngl
        LLAMACPP_PARALLEL                  how many requests at once, -np
        LLAMACPP_API_KEY                   --api-key, for a non-loopback server

    Anything passed on the command line wins over .env, which is what makes
    trying a second GGUF one flag rather than an edit.

    -Model names one of the GGUFs this project has been run on, smallest first:

        qwen3-4b        4B    ~2.5 GB   the default, fits a 4 GB card whole
        qwen3-8b        8B    ~5.0 GB   noticeably better at multi-step tool use
        qwen3-14b      14B    ~9.0 GB   better again, wants ~10 GB of VRAM
        qwen3-30b-a3b  30B   ~18.6 GB   MoE, 3B active - the strongest here

    None of them replaces another: they sit side by side in var\models and
    -Model picks which one this server loads. -List shows the table with what is
    already downloaded, and -Download fetches the one you asked for.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\llama-server.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\llama-server.ps1 `
        -Gguf var\models\Other-Model-Q4_K_M.gguf -Alias other-model -Ctx 4096

.EXAMPLE
    # The bigger model, downloaded on first use and then loaded.
    powershell -ExecutionPolicy Bypass -File scripts\llama-server.ps1 `
        -Model qwen3-8b -Download

.EXAMPLE
    # What is on offer, and what is already on this machine.
    powershell -ExecutionPolicy Bypass -File scripts\llama-server.ps1 -List
#>

[CmdletBinding()]
param(
    # The GGUF to load. Defaults to LLAMACPP_GGUF, else the only .gguf in
    # var/models — which is the state a fresh checkout that followed .env is in.
    [string]$Gguf,

    # One of the names in the catalogue below - qwen3-4b, qwen3-8b, qwen3-14b,
    # qwen3-30b-a3b. A shorthand for the -Gguf path and the -Alias that goes
    # with it, so moving up a size is one flag rather than a filename and an
    # .env edit. -Gguf still wins where both are given.
    [string]$Model,

    # Fetch the -Model's GGUF into var\models if it is not there yet. Off by
    # default: these are multi-gigabyte files and a start script should not
    # begin a 9 GB download because somebody typed a name.
    [switch]$Download,

    # Print the catalogue - sizes, and which ones are already downloaded - and
    # exit without starting anything.
    [switch]$List,

    # The name the agents ask for. Must match LLAMACPP_MODEL, or the LLM screen
    # reports the model as not loaded — llama-server publishes this on
    # /v1/models and that is the list the screen offers.
    [string]$Alias,

    [int]$Ctx,
    [int]$Ngl,
    [int]$Port,

    # How many requests llama-server will work on at once (-np). One by
    # default, and that is a deliberate choice rather than a conservative one:
    # llama.cpp's own default is four, and four concurrent slots means four
    # compute buffers on the card. Two A2A agents talking to each other are
    # enough to open two of them at once, which is a CUDA out-of-memory on a
    # 4 GB card and takes the server down mid-negotiation. At one, the same
    # requests queue — a little slower, and it finishes.
    #
    # Raise it on a card with room to spare; the agents themselves never
    # notice either way.
    [int]$Parallel,

    # What the KV cache is stored as (-ctk/-ctv). q8_0 rather than llama.cpp's
    # own f16 default, because on a small card the cache is what decides
    # whether the model sits on the GPU at all. Qwen3-4B at -c 8192 wants
    # 1.13 GiB of f16 cache; 2.33 GiB of weights plus that plus the compute
    # buffer is more than a 4 GB card has, so llama.cpp leaves two thirds of
    # the layers in system RAM and generation drops to ~3 tok/s. The same
    # cache at q8_0 is 0.56 GiB, every layer fits, and it is ~27 tok/s.
    #
    # f16 turns it off; q4_0 halves it again to make room for a longer -c.
    [string]$CacheType,

    # Flash attention, -fa on|off|auto. On rather than llama.cpp's 'auto',
    # because a quantised V cache is only implemented on that path - see the
    # check below - and it shrinks the attention scratch buffer that has to
    # fit beside the weights.
    [string]$FlashAttn,

    # How many prompt tokens are handed to the backend at once (-b) and how
    # many of those are computed in one pass (-ub). The two knobs that decide
    # how long the first answer of an errand takes: the brief and the tool
    # schemas are ~3,200 tokens, and every one of them is read before the model
    # writes a character.
    #
    # -ub is the one that costs VRAM. Its scratch buffer sits beside the
    # weights, so a bigger physical batch is a faster prefill bought with
    # layers that no longer fit on the card - at -ub 2048 on a 4 GB GTX 1650
    # llama.cpp can only keep 11 of 36 layers there, against 25 at -ub 256, and
    # what the prefill gains the generation gives straight back. 256 is a
    # deliberate small default for that reason and not a conservative one.
    [int]$Batch,
    [int]$UBatch,

    # Do the prompt's big matrix multiplies through cuBLAS rather than
    # llama.cpp's own quantised kernels (GGML_CUDA_FORCE_CUBLAS=1).
    #
    # Those kernels are written for the int8 tensor cores that a GTX 16xx does
    # not have, and without them the card prefills at about the speed of the
    # CPU: measured here, 62 tokens/s against 126 through cuBLAS on the same
    # 3,072-token prompt, with generation a shade faster too. On a card that
    # does have tensor cores the reverse is true, which is why this is a switch
    # rather than something set for everybody.
    [switch]$ForceCublas,

    # llama-server.exe. Defaults to var/llamacpp, where .env says to unzip a
    # release; falls back to whatever is on PATH.
    [string]$Exe,

    # Chat templates come from the GGUF's own metadata rather than llama.cpp's
    # built-in list. This is what makes tool calls work on a model whose
    # template emits them, so it is on unless deliberately turned off.
    [switch]$NoJinja,

    # Ask a hybrid model not to think before it answers (--reasoning-budget 0).
    # qwen3-8b and qwen3-14b deliberate by default, which is tokens and seconds
    # spent before every one of the twenty-odd tool calls an errand makes; the
    # 4B and the 30B-A3B here are instruct-only builds and ignore this. Needs a
    # llama.cpp build from mid-2025 or later, which is where the flag landed.
    [switch]$NoThink
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$modelsDir = Join-Path $root 'var\models'

# --- the models this project has been run on -------------------------------- #
# A ladder rather than a replacement: the 4B is still the default and still the
# one a 4 GB card holds whole, and the three above it are there for a machine
# with more to give. Every one of them is a Q4_K_M GGUF whose own chat template
# emits tool calls under --jinja, which is the only property the agents need -
# nothing in agent/ knows or cares which of these is loaded, because the model
# list comes back off /v1/models rather than out of a file.
#
# Size is the download, and Vram is roughly what it takes to sit entirely on the
# card at -ngl 99. Less than that is not a failure: llama.cpp splits the layers
# and keeps the rest in system RAM, which is slower and still correct.
$catalog = [ordered]@{
    'qwen3-4b' = @{
        File   = 'Qwen3-4B-Instruct-2507-Q4_K_M.gguf'
        Alias  = 'qwen3-4b-instruct-2507'
        Params = '4B'
        Size   = '2.5 GB'
        Vram   = '~3.5 GB'
        Note   = 'The default. Fits a 4 GB card whole; instruct-only, so no thinking to turn off.'
        Url    = 'https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf'
    }
    'qwen3-8b' = @{
        File   = 'Qwen3-8B-Q4_K_M.gguf'
        Alias  = 'qwen3-8b'
        Params = '8B'
        Size   = '5.0 GB'
        Vram   = '~6.5 GB'
        Note   = 'Steadier over a long tool-calling errand than the 4B. Hybrid thinker - see -NoThink.'
        Url    = 'https://huggingface.co/unsloth/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf'
    }
    'qwen3-14b' = @{
        File   = 'Qwen3-14B-Q4_K_M.gguf'
        Alias  = 'qwen3-14b'
        Params = '14B'
        Size   = '9.0 GB'
        Vram   = '~10.5 GB'
        Note   = 'Better again at holding a plan across steps. Hybrid thinker - see -NoThink.'
        Url    = 'https://huggingface.co/unsloth/Qwen3-14B-GGUF/resolve/main/Qwen3-14B-Q4_K_M.gguf'
    }
    'qwen3-30b-a3b' = @{
        File   = 'Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf'
        Alias  = 'qwen3-30b-a3b'
        Params = '30B (3B active)'
        Size   = '18.6 GB'
        Vram   = '~20 GB'
        Note   = 'A mixture of experts: 30B of weights, 3B of them per token, so it stays quick even split across CPU and GPU. The strongest of the four.'
        Url    = 'https://huggingface.co/unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF/resolve/main/Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf'
    }
}

if ($List) {
    Write-Host ""
    Write-Host "  llama.cpp models this project knows, smallest first."
    Write-Host "  Pick one with -Model <name>; add -Download to fetch it."
    Write-Host ""
    Write-Host ("  {0,-14} {1,-16} {2,-9} {3,-10} {4}" -f 'NAME', 'PARAMETERS', 'DOWNLOAD', 'VRAM', 'ON THIS MACHINE')
    foreach ($entry in $catalog.GetEnumerator()) {
        $present = Test-Path (Join-Path $modelsDir $entry.Value.File)
        Write-Host ("  {0,-14} {1,-16} {2,-9} {3,-10} {4}" -f `
            $entry.Key, $entry.Value.Params, $entry.Value.Size, $entry.Value.Vram, `
            $(if ($present) { 'downloaded' } else { '-' }))
    }
    Write-Host ""
    foreach ($entry in $catalog.GetEnumerator()) {
        Write-Host ("  {0,-14} {1}" -f $entry.Key, $entry.Value.Note)
    }
    Write-Host ""
    Write-Host "  Any other GGUF works too - drop it in var\models and pass -Gguf."
    Write-Host ""
    return
}

function Get-Gguf {
    <#
      Fetch one catalogue entry into var\models.

      It downloads to a .part file and renames on success, so an interrupted
      download is never left looking like a loadable model - and `curl -C -`
      picks the same .part up where it stopped rather than starting the 9 GB
      over.
    #>
    param([hashtable]$Entry, [string]$Destination)

    $curl = Get-Command 'curl.exe' -ErrorAction SilentlyContinue
    if (-not $curl) {
        throw @"
curl.exe was not found, so this script cannot fetch the model for you. Download
it by hand into var\models:

  $($Entry.Url)
"@
    }

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
    $partial = "$Destination.part"

    Write-Host ""
    Write-Host "  downloading  $($Entry.File)  ($($Entry.Size))"
    Write-Host "  from         $($Entry.Url)"
    Write-Host ""

    & $curl.Source -L -C - --fail --retry 3 -o $partial $Entry.Url
    if ($LASTEXITCODE -ne 0) {
        throw "Download failed (curl exit $LASTEXITCODE). Run the same command again - it resumes from $([System.IO.Path]::GetFileName($partial))."
    }
    Move-Item -LiteralPath $partial -Destination $Destination -Force
    Write-Host ""
    Write-Host "  downloaded   $Destination"
}


# --- .env, read the way agent/config.py reads it ---------------------------- #
# Blank means "not set", so an empty value in .env falls through to the default
# here exactly as it does in Python.
$envFile = Join-Path $root '.env'
$fromEnv = @{}
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        $text = $line.Trim()
        if ($text -eq '' -or $text.StartsWith('#')) { continue }
        $split = $text.IndexOf('=')
        if ($split -lt 1) { continue }
        $name = $text.Substring(0, $split).Trim()
        $value = $text.Substring($split + 1).Trim().Trim('"')
        if ($value -ne '') { $fromEnv[$name] = $value }
    }
}

function Get-Setting {
    param([string[]]$Names, [string]$Default = '')
    foreach ($name in $Names) {
        if ($fromEnv.ContainsKey($name)) { return $fromEnv[$name] }
        $live = [Environment]::GetEnvironmentVariable($name)
        if ($live) { return $live }
    }
    return $Default
}

# --- where to listen -------------------------------------------------------- #
# The port comes out of the same URL the agents dial, so the two cannot drift.
if (-not $Port) {
    $baseUrl = Get-Setting @('LLAMACPP_BASE_URL', 'LLM_BASE_URL') 'http://localhost:8080'
    try {
        $Port = ([Uri]$baseUrl).Port
    } catch {
        throw "LLM_BASE_URL is not a URL this script can read a port out of: $baseUrl"
    }
}

# --- what to load ----------------------------------------------------------- #
# Four ways to say it, in the order they win: -Gguf (any path at all), -Model (a
# name from the catalogue), LLAMACPP_GGUF, and failing all three the only .gguf
# in var\models - which is the state a fresh checkout that followed .env is in.
$preset = $null
if ($Model) {
    $preset = $catalog[$Model.ToLowerInvariant()]
    if (-not $preset) {
        $names = ($catalog.Keys) -join ', '
        throw "Unknown -Model '$Model'. The names this script knows are: $names. Run it with -List for the table, or pass -Gguf for any other GGUF."
    }
    if (-not $Gguf) { $Gguf = Join-Path $modelsDir $preset.File }
}
if (-not $Gguf) { $Gguf = Get-Setting @('LLAMACPP_GGUF') }
if (-not $Gguf) {
    $found = @(if (Test-Path $modelsDir) { Get-ChildItem -Path $modelsDir -Filter '*.gguf' -File })
    if ($found.Count -eq 1) {
        $Gguf = $found[0].FullName
    } elseif ($found.Count -eq 0) {
        throw @"
No GGUF to load. Put one in var\models, or set LLAMACPP_GGUF in .env. The four
this script knows are listed by:

  powershell -ExecutionPolicy Bypass -File scripts\llama-server.ps1 -List

and any one of them is fetched and started with, for example:

  powershell -ExecutionPolicy Bypass -File scripts\llama-server.ps1 -Model qwen3-4b -Download
"@
    } else {
        # More than one is the normal state once a second model has been pulled,
        # so this asks which rather than complaining that there are two.
        $names = ($found | ForEach-Object { $_.Name }) -join ', '
        throw "var\models holds more than one GGUF ($names). Say which: -Model <name> (see -List), -Gguf <path>, or LLAMACPP_GGUF in .env."
    }
}
if (-not [System.IO.Path]::IsPathRooted($Gguf)) { $Gguf = Join-Path $root $Gguf }
if (-not (Test-Path $Gguf)) {
    # A catalogue model that has not been downloaded yet is the one missing file
    # this script can do something about.
    if ($preset -and $Download) {
        Get-Gguf -Entry $preset -Destination $Gguf
    } elseif ($preset) {
        throw @"
$($preset.File) is not in var\models yet ($($preset.Size) to download).

  powershell -ExecutionPolicy Bypass -File scripts\llama-server.ps1 -Model $($Model.ToLowerInvariant()) -Download

or fetch it yourself:

  curl -L -o var/models/$($preset.File) $($preset.Url)
"@
    } else {
        throw "No such GGUF: $Gguf"
    }
}

# --- the rest --------------------------------------------------------------- #
# The alias the model is published under. A catalogue model brings its own, so
# `-Model qwen3-8b` is a whole switch of model rather than a new file served
# under the old name; anything typed on the command line still wins.
$envAlias = Get-Setting @('LLAMACPP_MODEL')
if (-not $Alias) {
    # Only when the preset's own file is what is being loaded: -Model with an
    # explicit -Gguf is somebody serving a different file, and labelling it with
    # the catalogue name would put a name on the LLM screen that is not what is
    # answering.
    if ($preset -and -not $PSBoundParameters.ContainsKey('Gguf')) {
        $Alias = $preset.Alias
    } else {
        $Alias = $envAlias
    }
}
if (-not $Alias) { $Alias = 'default' }
if (-not $Ctx) { $Ctx = [int](Get-Setting @('LLAMACPP_CTX') '8192') }
if (-not $PSBoundParameters.ContainsKey('Ngl')) { $Ngl = [int](Get-Setting @('LLAMACPP_NGL') '99') }
if (-not $Parallel) { $Parallel = [int](Get-Setting @('LLAMACPP_PARALLEL') '1') }
if (-not $CacheType) { $CacheType = Get-Setting @('LLAMACPP_CACHE_TYPE') 'q8_0' }
if (-not $FlashAttn) { $FlashAttn = Get-Setting @('LLAMACPP_FLASH_ATTN') 'on' }
if (-not $Batch) { $Batch = [int](Get-Setting @('LLAMACPP_BATCH') '2048') }
if (-not $UBatch) { $UBatch = [int](Get-Setting @('LLAMACPP_UBATCH') '256') }
if (-not $ForceCublas) {
    $ForceCublas = (Get-Setting @('LLAMACPP_FORCE_CUBLAS') 'false') -match '^(1|true|yes|on)$'
}

# A quantised V cache is only implemented on the flash-attention path. Setting
# one without the other does not give a slower server: llama-server fails to
# create the context and exits, which reaches the operator as the autostart
# timeout rather than as a reason. Give the reason here instead.
if ($FlashAttn -eq 'off' -and $CacheType -ne 'f16') {
    throw "LLAMACPP_CACHE_TYPE=$CacheType needs flash attention - llama.cpp only quantises the V cache on that path. Either LLAMACPP_FLASH_ATTN=on, or LLAMACPP_CACHE_TYPE=f16, and note that an f16 cache at -c $Ctx does not fit a 4 GB card beside the weights."
}
$apiKey = Get-Setting @('LLAMACPP_API_KEY')

if (-not $Exe) {
    $bundled = Join-Path $root 'var\llamacpp\llama-server.exe'
    if (Test-Path $bundled) {
        $Exe = $bundled
    } else {
        $onPath = Get-Command 'llama-server' -ErrorAction SilentlyContinue
        if (-not $onPath) {
            throw @"
llama-server.exe was not found in var\llamacpp and is not on PATH.

Download a build for this machine from https://github.com/ggml-org/llama.cpp/releases
and unzip it into var\llamacpp — for an NVIDIA card that is the cuda build plus
the matching cudart archive; with no GPU, the -bin-win-cpu-x64 one.
"@
        }
        $Exe = $onPath.Source
    }
}

# --- go --------------------------------------------------------------------- #
# --host 127.0.0.1 rather than 0.0.0.0: this serves the agents on this machine,
# and a model server with no credential should not be listening to the network.
$arguments = @(
    '-m', $Gguf,
    '-a', $Alias,
    '--host', '127.0.0.1',
    '--port', $Port,
    '-c', $Ctx,
    '-ngl', $Ngl,
    '-np', $Parallel,
    '-ctk', $CacheType,
    '-ctv', $CacheType,
    '-fa', $FlashAttn,
    '-b', $Batch,
    '-ub', $UBatch
)
if (-not $NoJinja) { $arguments += '--jinja' }
if ($NoThink) { $arguments += @('--reasoning-budget', '0') }
if ($apiKey) { $arguments += @('--api-key', $apiKey) }

Write-Host ""
Write-Host "  llama.cpp   $(Split-Path -Leaf $Exe)"
Write-Host "  model       $(Split-Path -Leaf $Gguf)  (as '$Alias')"
Write-Host "  listening   http://localhost:$Port"
Write-Host "  context     $Ctx tokens, $Ngl layers on the GPU"
Write-Host "  batch       $Batch logical, $UBatch physical"
Write-Host "  kv cache    $CacheType, flash attention $FlashAttn"
Write-Host "  slots       $Parallel request(s) at a time"
if ($NoThink) { Write-Host "  thinking    off (--reasoning-budget 0)" }
if ($ForceCublas) { Write-Host "  matmul      cuBLAS (no int8 tensor cores on this card)" }
Write-Host ""
# The agents ask for a model by name, and the name they ask for is whatever the
# LLM screen or .env says. Worth saying out loud when this server is about to
# publish a different one.
if ($envAlias -and $envAlias -ne $Alias) {
    Write-Host "  Note: LLAMACPP_MODEL in .env is '$envAlias', and this server is"
    Write-Host "  publishing '$Alias'. Pick it on the LLM Configuration screen, or"
    Write-Host "  set LLAMACPP_MODEL=$Alias in .env."
    Write-Host ""
}
Write-Host "  Leave this running. Then pick llama.cpp on the LLM Configuration"
Write-Host "  screen, or set LLM_PROVIDER=llamacpp in .env."
Write-Host ""

# Read by the CUDA backend as it initialises, so it has to be in the
# environment before the process starts rather than on its command line.
if ($ForceCublas) { $env:GGML_CUDA_FORCE_CUBLAS = '1' }

& $Exe @arguments
