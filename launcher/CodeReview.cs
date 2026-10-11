/*
 * CodeReview.exe —— 代码审查 Agent 的启动器（C# 源码）
 *
 * 为什么需要一个启动器：
 *   Agent 本体是 Python，但用户双击的不该是 .py（会被系统用别的程序打开，
 *   也不方便带参数）。这个 exe 负责找到解释器、拼好命令行、把控制台原样交给
 *   Python，并把退出码透传给调用方。
 *
 * 解释器查找顺序：
 *   1. <exe 目录>\runtime\python.exe   —— 随包分发的精简运行时（免安装的关键）
 *   2. 系统 PATH 里的 python.exe        —— 退路，方便没带 runtime 的场景
 *
 * 编译方式（见 build.ps1）：
 *   csc /target:exe /out:CodeReview.exe /codepage:65001 launcher\CodeReview.cs
 *
 * 刻意只用 C# 5 语法：随 Windows 自带的 .NET Framework 编译器（csc.exe）
 * 最高只认到 C# 5，用新语法会直接编不过。
 */

using System;
using System.Diagnostics;
using System.IO;
using System.Text;

internal static class CodeReviewLauncher
{
    private const string AppName = "CodeReview";

    private static int Main(string[] args)
    {
        TrySetTitle(AppName + " - 代码错误审查 Agent");

        string baseDir = AppDomain.CurrentDomain.BaseDirectory;
        if (string.IsNullOrEmpty(baseDir))
        {
            baseDir = Directory.GetCurrentDirectory();
        }
        baseDir = baseDir.TrimEnd('\\', '/');

        // ---- 1. 程序文件必须在 exe 旁边 ---------------------------------- //
        string mainPy = Path.Combine(baseDir, "main.py");
        if (!File.Exists(mainPy))
        {
            return Fail(
                "找不到程序文件 main.py。\n" +
                "\n" +
                "  期望位置：" + mainPy + "\n" +
                "\n" +
                "请确认 CodeReview.exe、main.py、codeagent\\ 与 runtime\\\n" +
                "都在同一个目录里（整个文件夹一起拷贝，不要只拷 exe）。"
            );
        }

        // ---- 2. 找解释器 -------------------------------------------------- //
        string python = FindPython(baseDir);
        if (python == null)
        {
            return Fail(
                "找不到可用的 Python 解释器。\n" +
                "\n" +
                "  已尝试：\n" +
                "    1. " + Path.Combine(baseDir, "runtime", "python.exe") + "\n" +
                "    2. 系统 PATH 中的 python.exe\n" +
                "\n" +
                "若是拷贝来的程序，请确认 runtime\\ 目录完整；\n" +
                "或者自行安装 Python 3.10+ 并确保 python 在 PATH 中。"
            );
        }

        // ---- 3. 组装命令行 ------------------------------------------------ //
        ProcessStartInfo psi = new ProcessStartInfo();
        psi.FileName = python;
        psi.Arguments = Quote(mainPy) + (args.Length > 0 ? " " + JoinArgs(args) : "");
        // 继承调用者的当前目录：用户在哪个项目里执行，就审查哪个项目。
        psi.WorkingDirectory = Directory.GetCurrentDirectory();
        psi.UseShellExecute = false;
        psi.CreateNoWindow = false;
        // 关掉 Python 的输出缓冲，让流式回答能立即显示而不是攒到最后。
        psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";

        try
        {
            Process proc = Process.Start(psi);
            if (proc == null)
            {
                return Fail("无法启动 Python 进程。");
            }
            proc.WaitForExit();
            int exitCode = proc.ExitCode;
            proc.Close();

            // 双击启动时窗口会在进程退出后立刻消失，出错信息就看不到。
            // 因此只在**非零退出**且**处于真实控制台**时暂停一下。
            if (exitCode != 0 && !Console.IsInputRedirected && !Console.IsOutputRedirected)
            {
                Console.WriteLine();
                Console.WriteLine("  [退出码 " + exitCode + "] 按回车键关闭窗口...");
                try { Console.ReadLine(); } catch (Exception) { }
            }
            return exitCode;
        }
        catch (Exception ex)
        {
            return Fail("启动 Python 失败：" + ex.Message);
        }
    }

    // ------------------------------------------------------------------ //
    // 解释器查找
    // ------------------------------------------------------------------ //
    private static string FindPython(string baseDir)
    {
        string bundled = Path.Combine(Path.Combine(baseDir, "runtime"), "python.exe");
        if (IsUsableExe(bundled))
        {
            return bundled;
        }

        string pathVar = Environment.GetEnvironmentVariable("PATH");
        if (string.IsNullOrEmpty(pathVar))
        {
            return null;
        }
        string[] entries = pathVar.Split(';');
        for (int i = 0; i < entries.Length; i++)
        {
            string dir = entries[i].Trim().Trim('"');
            if (dir.Length == 0)
            {
                continue;
            }
            try
            {
                string candidate = Path.Combine(dir, "python.exe");
                if (IsUsableExe(candidate))
                {
                    return candidate;
                }
            }
            catch (Exception)
            {
                // 路径非法就跳过，不影响继续找
            }
        }
        return null;
    }

    /// <summary>
    /// 判断是不是一个真的可执行文件。
    /// Windows 的 Microsoft Store 会放一个 0 字节的 python.exe 占位存根，
    /// <c>File.Exists</c> 对它返回 true，但执行它只会弹应用商店 —— 必须按体积排掉。
    /// </summary>
    private static bool IsUsableExe(string path)
    {
        try
        {
            FileInfo info = new FileInfo(path);
            return info.Exists && info.Length > 4096;
        }
        catch (Exception)
        {
            return false;
        }
    }

    // ------------------------------------------------------------------ //
    // 命令行拼接
    // ------------------------------------------------------------------ //
    private static string JoinArgs(string[] args)
    {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < args.Length; i++)
        {
            if (i > 0)
            {
                sb.Append(' ');
            }
            sb.Append(Quote(args[i]));
        }
        return sb.ToString();
    }

    /// <summary>
    /// 按 Windows 的命令行解析规则给参数加引号。
    /// 规则比看上去绕：反斜杠只有在引号之前才需要翻倍，因此要逐字符扫描。
    /// </summary>
    private static string Quote(string arg)
    {
        if (arg.Length > 0 && arg.IndexOfAny(new char[] { ' ', '\t', '"' }) < 0)
        {
            return arg;
        }

        StringBuilder sb = new StringBuilder();
        sb.Append('"');
        int backslashes = 0;
        for (int i = 0; i < arg.Length; i++)
        {
            char c = arg[i];
            if (c == '\\')
            {
                backslashes++;
                continue;
            }
            if (c == '"')
            {
                sb.Append('\\', backslashes * 2 + 1);
                sb.Append('"');
                backslashes = 0;
                continue;
            }
            if (backslashes > 0)
            {
                sb.Append('\\', backslashes);
                backslashes = 0;
            }
            sb.Append(c);
        }
        if (backslashes > 0)
        {
            sb.Append('\\', backslashes * 2);
        }
        sb.Append('"');
        return sb.ToString();
    }

    // ------------------------------------------------------------------ //
    // 出错提示
    // ------------------------------------------------------------------ //
    private static int Fail(string message)
    {
        Console.WriteLine();
        Console.WriteLine("  " + AppName + " 启动失败");
        Console.WriteLine("  " + new string('-', 58));
        string[] lines = message.Split('\n');
        for (int i = 0; i < lines.Length; i++)
        {
            Console.WriteLine("  " + lines[i].TrimEnd('\r'));
        }
        Console.WriteLine();
        Console.WriteLine("  按回车键关闭...");
        try { Console.ReadLine(); } catch (Exception) { }
        return 3;
    }

    private static void TrySetTitle(string title)
    {
        try { Console.Title = title; } catch (Exception) { }
    }
}
