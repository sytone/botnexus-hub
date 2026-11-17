using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;

namespace BotNexus.Extensions.HubConnector;

internal readonly record struct HubCpuTimes(double Total, double Idle);
internal sealed record HubContainerObservation(string Source, double? CpuSeconds, double? CpuCapacity,
    double? MemoryLimit, double? MemoryUsed);
internal sealed record HubResourceObservation
{
    public double? CpuSeconds { get; init; }
    public int LogicalProcessors { get; init; } = 1;
    public double? RssBytes { get; init; }
    public double? PrivateBytes { get; init; }
    public double? HeapBytes { get; init; }
    public double? GcPausePercent { get; init; }
    public double? Threads { get; init; }
    public double? UptimeSeconds { get; init; }
    public double? ReadBytes { get; init; }
    public double? WriteBytes { get; init; }
    public HubCpuTimes? MachineCpu { get; init; }
    public double? MachineTotalMemory { get; init; }
    public double? MachineAvailableMemory { get; init; }
    public double? DiskTotal { get; init; }
    public double? DiskFree { get; init; }
    public HubContainerObservation? Container { get; init; }
}
internal interface IHubResourceProbe { HubResourceObservation Capture(); }

// Only the current process, fixed OS counters, and capacity of the application filesystem.
// No process enumeration, world paths, directory size scans, SQL, shell commands, or network access.
internal sealed class HubResourceProbe : IHubResourceProbe
{
    public HubResourceObservation Capture()
    {
        double? cpu = null, rss = null, privateBytes = null, threads = null, uptime = null, read = null, write = null;
        try
        {
            using var process = Process.GetCurrentProcess();
            cpu = Try(() => process.TotalProcessorTime.TotalSeconds);
            rss = Try(() => process.WorkingSet64);
            privateBytes = Try(() => process.PrivateMemorySize64);
            threads = Try(() => process.Threads.Count);
            uptime = Try(() => (DateTimeOffset.UtcNow - new DateTimeOffset(process.StartTime.ToUniversalTime())).TotalSeconds);
            if (OperatingSystem.IsWindows() && GetProcessIoCounters(process.Handle, out var io))
            {
                read = io.ReadTransferCount;
                write = io.WriteTransferCount;
            }
        }
        catch (Exception) { }
        HubCpuTimes? machineCpu = null;
        double? total = null, available = null;
        HubContainerObservation? container = null;
        if (OperatingSystem.IsLinux())
        {
            var stat = ReadText("/proc/stat");
            machineCpu = HubLinuxMetrics.Cpu(stat);
            (total, available) = HubLinuxMetrics.Memory(ReadText("/proc/meminfo"));
            (read, write) = HubLinuxMetrics.Io(ReadText("/proc/self/io"));
            container = HubLinuxMetrics.Container(ReadText, total);
        }
        else if (OperatingSystem.IsWindows())
        {
            try
            {
                // GetSystemTimes covers only one processor group above 64 CPUs. Omit rather than claim host-wide CPU.
                if (GetActiveProcessorCount(ushort.MaxValue) is > 0 and <= 64
                    && GetSystemTimes(out var idle, out var kernel, out var user))
                    machineCpu = new((double)kernel.Value + user.Value, idle.Value);
                var memory = new MemoryStatus { Length = (uint)Marshal.SizeOf<MemoryStatus>() };
                if (GlobalMemoryStatusEx(ref memory)) { total = memory.TotalPhysical; available = memory.AvailablePhysical; }
            }
            catch (Exception) { }
        }
        double? diskTotal = null, diskFree = null;
        try
        {
            var path = Path.GetFullPath(AppContext.BaseDirectory);
            if (OperatingSystem.IsWindows())
            {
                // Directory mounts and UNC shares need a path query, not a drive-letter assumption.
                if (GetDiskFreeSpaceEx(path, out _, out var capacity, out var free))
                { diskTotal = capacity; diskFree = free; }
            }
            else
            {
                // On Unix DriveInfo queries statfs/statvfs for this exact path. That handles
                // nested mounts and symlinks without enumerating drives or scanning directory contents.
                var drive = new DriveInfo(path);
                diskTotal = drive.TotalSize;
                diskFree = drive.TotalFreeSpace;
            }
        }
        catch (Exception) { }
        return new()
        {
            CpuSeconds = cpu, LogicalProcessors = Math.Max(1, Environment.ProcessorCount), RssBytes = rss,
            PrivateBytes = privateBytes, HeapBytes = Try(() => GC.GetTotalMemory(forceFullCollection: false)),
            // Runtime's running pause percentage, updated by each GC; not a fabricated per-interval delta.
            GcPausePercent = Try(() => GC.GetGCMemoryInfo().PauseTimePercentage), Threads = threads, UptimeSeconds = uptime,
            ReadBytes = read, WriteBytes = write, MachineCpu = machineCpu, MachineTotalMemory = total,
            MachineAvailableMemory = available, DiskTotal = diskTotal, DiskFree = diskFree, Container = container
        };
    }
    private static double? Try(Func<double> value)
    {
        try { var number = value(); return HubMetricCatalog.Number(number) ? number : null; }
        catch (Exception) { return null; }
    }
    internal static string? ReadText(string path)
    {
        try
        {
            using var reader = new StreamReader(path);
            var buffer = new char[262_145];
            var length = reader.ReadBlock(buffer, 0, buffer.Length);
            return length <= 262_144 ? new string(buffer, 0, length) : null;
        }
        catch (Exception) { return null; }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct FileTime
    {
        public uint Low;
        public uint High;
        public readonly ulong Value => ((ulong)High << 32) | Low;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
        public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct MemoryStatus
    {
        public uint Length, MemoryLoad;
        public ulong TotalPhysical, AvailablePhysical, TotalPageFile, AvailablePageFile;
        public ulong TotalVirtual, AvailableVirtual, AvailableExtendedVirtual;
    }
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetSystemTimes(out FileTime idle, out FileTime kernel, out FileTime user);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint GetActiveProcessorCount(ushort group);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetProcessIoCounters(IntPtr process, out IoCounters counters);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GlobalMemoryStatusEx(ref MemoryStatus memory);
    [DllImport("kernel32.dll", EntryPoint = "GetDiskFreeSpaceExW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetDiskFreeSpaceEx(string directory, out ulong available, out ulong total, out ulong free);
}

internal sealed class HubResourceRates(TimeSpan interval)
{
    private HubResourceObservation? _previous;
    private double _previousAt;
    public IReadOnlyList<HubMetricPoint> Observe(HubResourceObservation current, double now)
    {
        var points = new List<HubMetricPoint>();
        void Add(string id, double? value)
        {
            if (value is { } number && HubMetricCatalog.Number(number) && (!HubMetricCatalog.IsPercent(id) || number <= 100))
                points.Add(new(id, number));
        }
        var elapsed = now - _previousAt;
        var validInterval = _previous is not null && double.IsFinite(elapsed) && elapsed > 0 && elapsed <= interval.TotalSeconds * 2.5;
        double? Rate(double? previous, double? next) => validInterval && previous is { } before && next is { } after
            && HubMetricCatalog.Number(before) && HubMetricCatalog.Number(after) && after >= before ? (after - before) / elapsed : null;
        double? Percent(double? value) => value is { } number && double.IsFinite(number) ? Math.Clamp(number, 0, 100) : null;
        if (_previous is { } previous)
        {
            if (previous.LogicalProcessors == current.LogicalProcessors && current.LogicalProcessors > 0)
                Add("gateway.cpu.percent", Percent(Rate(previous.CpuSeconds, current.CpuSeconds) * 100 / current.LogicalProcessors));
            Add("gateway.io.read_bytes_per_second", Rate(previous.ReadBytes, current.ReadBytes));
            Add("gateway.io.write_bytes_per_second", Rate(previous.WriteBytes, current.WriteBytes));
            if (validInterval && previous.MachineCpu is { } oldCpu && current.MachineCpu is { } newCpu
                && HubMetricCatalog.Number(oldCpu.Total) && HubMetricCatalog.Number(oldCpu.Idle)
                && HubMetricCatalog.Number(newCpu.Total) && HubMetricCatalog.Number(newCpu.Idle)
                && newCpu.Total > oldCpu.Total && newCpu.Idle >= oldCpu.Idle
                && newCpu.Idle - oldCpu.Idle <= newCpu.Total - oldCpu.Total)
                Add("machine.cpu.percent", 100 * (1 - (newCpu.Idle - oldCpu.Idle) / (newCpu.Total - oldCpu.Total)));
            if (previous.Container is { } oldContainer && current.Container is { } nextContainer
                && oldContainer.Source == nextContainer.Source && oldContainer.CpuCapacity == nextContainer.CpuCapacity
                && nextContainer.CpuCapacity is > 0)
                Add("container.cpu.percent", Percent(Rate(oldContainer.CpuSeconds, nextContainer.CpuSeconds) * 100 / nextContainer.CpuCapacity));
        }
        Add("gateway.memory.rss_bytes", current.RssBytes);
        Add("gateway.memory.private_bytes", current.PrivateBytes);
        Add("gateway.gc.heap_bytes", current.HeapBytes);
        Add("gateway.gc.pause_percent", current.GcPausePercent);
        Add("gateway.threads.count", current.Threads);
        Add("gateway.uptime.seconds", current.UptimeSeconds);
        if (current.MachineTotalMemory is > 0 && current.MachineAvailableMemory is >= 0
            && current.MachineAvailableMemory <= current.MachineTotalMemory)
        {
            Add("machine.memory.total_bytes", current.MachineTotalMemory);
            Add("machine.memory.used_bytes", current.MachineTotalMemory - current.MachineAvailableMemory);
            Add("machine.memory.percent", (current.MachineTotalMemory - current.MachineAvailableMemory) * 100 / current.MachineTotalMemory);
        }
        if (current.DiskTotal is > 0 && current.DiskFree is >= 0 && current.DiskFree <= current.DiskTotal)
        {
            Add("machine.disk.total_bytes", current.DiskTotal);
            Add("machine.disk.free_bytes", current.DiskFree);
            Add("machine.disk.percent", (current.DiskTotal - current.DiskFree) * 100 / current.DiskTotal);
        }
        if (current.Container is { } container)
        {
            Add("container.memory.used_bytes", container.MemoryUsed);
            Add("container.memory.total_bytes", container.MemoryLimit);
            // A cgroup may briefly exceed its hard limit. Retain the byte values, cap only the percentage.
            if (container.MemoryLimit is > 0 && container.MemoryUsed is >= 0)
                Add("container.memory.percent", Percent(container.MemoryUsed * 100 / container.MemoryLimit));
        }
        _previous = current;
        _previousAt = now;
        return points;
    }
    public void Reset() => _previous = null;
}

internal static class HubLinuxMetrics
{
    private static string[] Fields(string text) => text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
    internal static double? Unsigned(string? text)
        => ulong.TryParse(text?.Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out var value) && value <= 1e18 ? value : null;
    internal static HubCpuTimes? Cpu(string? text)
    {
        var line = text?.Split('\n').FirstOrDefault(row => row.StartsWith("cpu ", StringComparison.Ordinal));
        if (line is null) return null;
        var fields = Fields(line);
        if (fields.Length < 5) return null;
        var numbers = fields.Skip(1).Take(8).Select(Unsigned).ToArray(); // guest and guest_nice already belong to user/nice.
        if (numbers.Any(number => number is null)) return null;
        var total = numbers.Sum(number => number!.Value);
        var idle = numbers[3]!.Value + (numbers.Length > 4 ? numbers[4]!.Value : 0);
        return HubMetricCatalog.Number(total) && total >= idle ? new(total, idle) : null;
    }
    private static double? Key(string? text, string key)
    {
        if (text is null) return null;
        foreach (var line in text.Split('\n'))
        {
            var fields = Fields(line);
            if (fields.Length >= 2 && fields[0].TrimEnd(':') == key) return Unsigned(fields[1]);
        }
        return null;
    }
    internal static (double? Total, double? Available) Memory(string? text)
    {
        double? Kilobytes(string key)
        {
            var line = text?.Split('\n').FirstOrDefault(row => row.StartsWith(key + ":", StringComparison.Ordinal));
            if (line is null) return null;
            var fields = Fields(line);
            return fields.Length == 3 && fields[2] == "kB" ? Unsigned(fields[1]) * 1024 : null;
        }
        // Missing MemAvailable is unavailable, not MemFree or a guessed cache formula.
        return (Kilobytes("MemTotal"), Kilobytes("MemAvailable"));
    }
    internal static (double? Read, double? Write) Io(string? text) => (Key(text, "read_bytes"), Key(text, "write_bytes"));

    internal static HubContainerObservation? Container(Func<string, string?> read, double? hostTotal)
    {
        var location = CgroupDirectory(read("/proc/self/cgroup"), read("/proc/self/mountinfo"));
        if (location is null) return null;
        var (directory, source) = location.Value;
        var used = Unsigned(read(directory + "/memory.current"));
        var limit = Unsigned(read(directory + "/memory.max")); // "max" is unavailable, never host RAM as a fake limit.
        if (limit is <= 0 || hostTotal is > 0 && limit > hostTotal) limit = null;
        var fields = Fields(read(directory + "/cpu.max") ?? "");
        var quota = fields.Length == 2 ? Unsigned(fields[0]) : null;
        var period = fields.Length == 2 ? Unsigned(fields[1]) : null;
        var capacity = quota is > 0 && period is > 0 ? quota / period : null;
        var seconds = Key(read(directory + "/cpu.stat"), "usage_usec") / 1_000_000;
        return used is null && limit is null && seconds is null ? null : new(source, seconds, capacity, limit, used);
    }

    // Resolve only the process's own v2 membership. A namespaced root needs a non-root mount root as proof;
    // a host root or an ambiguous namespace is omitted, not relabelled as container limits.
    internal static (string Directory, string Source)? CgroupDirectory(string? membership, string? mountInfo)
    {
        var path = membership?.Split('\n').FirstOrDefault(line => line.StartsWith("0::", StringComparison.Ordinal))?[3..].Trim();
        if (!SafePath(path) || mountInfo is null) return null;
        foreach (var line in mountInfo.Split('\n').Take(4096))
        {
            var halves = line.Split(" - ", 2, StringSplitOptions.None);
            if (halves.Length != 2 || !halves[1].StartsWith("cgroup2 ", StringComparison.Ordinal)) continue;
            var fields = Fields(halves[0]);
            if (fields.Length < 6) continue;
            var root = Unescape(fields[3]);
            var mount = Unescape(fields[4]);
            if (!SafePath(root) || !SafePath(mount)) continue;
            string? suffix = null;
            if (path == "/" && root != "/") suffix = "";
            else if (path != "/" && root == "/") suffix = path;
            else if (path == root && path != "/") suffix = "";
            else if (root != "/" && path!.StartsWith(root + "/", StringComparison.Ordinal)) suffix = path[root.Length..];
            if (suffix is null) continue;
            var directory = mount!.TrimEnd('/') + suffix;
            return (directory, root + "|" + path + "|" + mount);
        }
        return null;
    }
    private static bool SafePath(string? path) => path is not null && path.Length is > 0 and <= 4096
        && path.StartsWith('/') && !path.Any(char.IsControl) && !path.Contains('\\')
        && path.Split('/').All(part => part is not "." and not "..");
    private static string Unescape(string path) => path.Replace("\\040", " ", StringComparison.Ordinal)
        .Replace("\\011", "\t", StringComparison.Ordinal).Replace("\\012", "\n", StringComparison.Ordinal)
        .Replace("\\134", "\\", StringComparison.Ordinal);
}
