# HAT 衰减链 · 功放扫幅 / 扫频界面

这是一个中文桌面程序，用 VISA/SCPI 同时控制 RIGOL DG5352 Pro 和 DSA815，提供两个彼此独立的功能：

- **扫幅**：固定频率，每轮都从最大幅度扫到最小幅度；默认 2 轮，轮数、步长均可改。曲线分别显示各轮结果和多轮平均值。
- **扫频**：固定幅度，设置起止频率和频率步长；每个频点同步修改 DG 输出频率和 DSA 中心频率，结束后恢复扫频前的频率。

扫幅结束后，程序会在数据目录里另外生成平均 CSV、独立平均曲线 PNG 和高分辨率平均数据表 PNG；这些是独立文件，不会占用或改变测量界面。

每个点会：

1. 设置当前幅度或频率；
2. 等待功放和频谱稳定；
3. 让 DSA815 完成一次单次扫描；
4. 搜索当前迹线的最大峰值，读取峰值频率和峰值强度；
5. 加回五只外置衰减器的 48 dB；
6. 按 50 Ω 把发生器的 mVpp 换算成输入功率，再计算功率增益；
7. 实时画输出和增益曲线，并逐点写入 CSV。

## 已按你的接线设置的默认值

```text
功放 RF OUT
  → HAT-1A+   1 dB
  → HAT-2A+   2 dB
  → HAT-3A+   3 dB
  → HAT-30A+ 30 dB
  → HAT-12A+ 12 dB
  → RIGOL DSA815 RF INPUT
```

总外置衰减：`1 + 2 + 3 + 30 + 12 = 48 dB`。

扫幅的每一轮方向都是 `最大 → 最小`，不会来回折返。默认稳定等待为 `0.2 s`，每轮第一个点至少等待 `0.5 s`；每轮结束先关闭 RF，再重新从最大值开始。扫描完成、人工停止或发生异常时，都会关闭发生器 RF 输出。两个功能页都有独立可见的“RF 紧急关闭”按钮。

## 第一次运行

本机已经装有 Python、Matplotlib、PyVISA 和 NI-VISA，可以直接双击：

```text
双击启动.cmd
```

`start_gui.cmd` 是内容相同的英文文件名备用入口。

如果复制到另一台电脑后提示缺少 Python 包，先双击：

```text
install_dependencies.cmd
```

电脑还需要安装一个 VISA 后端，例如 NI-VISA。程序通过 PyVISA 使用 USB、LAN、GPIB 或串口 VISA 资源。

## 真机操作顺序

1. 在功放未上电、信号发生器 RF 输出关闭时，按上面的五级顺序接好衰减链。
2. 用 USB 或 LAN 把信号发生器和 DSA815 接到电脑。
3. 启动程序，先在默认“模拟仪器”模式跑一遍，确认界面、CSV 和曲线正常。
4. 程序启动后会自动扫描、识别并连接两台 VISA 仪器；连接成功后自动切换到“真实 VISA 仪器”模式，但不会自动开始扫幅。
5. 程序会对每个地址执行只读 `*IDN?`，自动把 DG 系列放入发生器栏、把 DSA 系列放入频谱仪栏；不会把同一地址同时填入两栏。需要重新检测时再点“扫描 VISA”。
6. 若某一栏显示“未发现”，先检查该仪器背面的 USB DEVICE、开机状态和数据线；LAN 设备可手工输入 `TCPIP0::仪器IP::INSTR`。
7. 使用扫幅时，在“扫幅测量（多轮）”页确认起点、终点、步长、轮数和 580 mVpp 硬上限，然后点“开始多轮扫幅”。
8. 扫幅结束后，可直接在 `measurements` 目录查看 `_average.csv`、`_average.png` 和 `_average_table.png`；最后一个文件是一张可放大查看的完整平均数据表图片。
9. 使用扫频时，在“扫频测量”页确认起止频率、频率步长和固定幅度，然后点“开始扫频”。扫幅参数和扫频参数互不覆盖。

## DG5352 Pro 怎么连接电脑

照片中的发生器是 **RIGOL DG5352 Pro**。它有两类 USB 口，作用不同：

- 正面左下角的 USB-A 是 **USB HOST**，用于 U 盘；不要拿它连接电脑。
- 电脑控制应使用后面板标着 **USB DEVICE** 的 USB-B 口。

因此，普通“双头 Type-C”线不能直接插到 DG5352 Pro 的 USB DEVICE 口。按电脑接口选择：

- 电脑有 USB-A：使用机器随附的 USB-A → USB-B 数据线；
- 电脑只有 USB-C：使用 USB-C → USB-B **数据线**；
- 或者使用 USB-C 扩展坞/转接头，再接随附的 USB-A → USB-B 线。

不要使用只能充电、不能传数据的线。RIGOL 手册建议连接通信线之前先关闭仪器；至少应先关闭 CH1 输出和功放，再接线。接好后：

1. 打开发生器和电脑；
2. Windows 应把它识别成 USB Test and Measurement Class（USBTMC）设备；
3. 打开本程序的“仪器连接 / SCPI”页，点“扫描 VISA”；
4. 选择名称类似 `USB0::...::INSTR` 的资源；
5. 点“连接并识别”，返回值应包含 `RIGOL TECHNOLOGIES,DG5352 Pro`。

若 USB 不方便，也可以把后面板 LAN 口接到与电脑同一局域网，在发生器的 `Utility → IO` 中查看 IP，然后在程序里手工输入：

```text
TCPIP0::发生器IP地址::INSTR
```

USB 和 LAN 二选一即可，不要同时为同一台仪器建立两条控制连接。

## DSA815 怎么连接电脑

DSA815 也必须使用后面板的 **USB DEVICE** 接口连接电脑，不能使用前面板的 USB HOST。进入频谱仪的 USB/I/O 设置，把 `Dev Class` 设为 `TMC`（通常为默认值）。Windows 正常识别后，扫描结果会出现第二个 `USB0::...::INSTR`，程序会根据 `*IDN?` 自动把它填入“频谱仪 VISA”栏。

若仍未出现，先把已确认可用的 DG5352 Pro 数据线临时换接到 DSA815：能识别说明第二根线或 Type-C 转接器有问题；仍不能识别则重点检查 DSA815 的 USB Device 设置、接口和开机状态。

## DG5352 Pro 的默认 SCPI

默认 SCPI 配置已按 DG5352 Pro 官方编程手册设置为 CH1、50 Ω 负载显示和 Vpp 单位：

```text
:OUTP1:LOAD 50
:SOUR1:VOLT:UNIT VPP
:SOUR1:VOLT {volts}
:SOUR1:FREQ {hz}
:OUTP1 ON
:OUTP1 OFF
```

如果以后换成其他信号发生器，直接在“仪器连接 / SCPI”页修改命令即可，不用改 Python。幅度模板支持：

- `{volts}`：例如 580 mVpp 会填成 `0.58`；
- `{mvpp}`：例如 580 mVpp 会填成 `580`。

频率模板支持 `{hz}` 和 `{mhz}`；默认使用 `:SOUR1:FREQ {hz}`。

开始真机扫描前，最好先在运行日志中确认命令和你的信号发生器编程手册一致。尤其要确认发生器显示的幅度定义为 **50 Ω 负载下的 Vpp**；如果发生器设成 High-Z，同一个屏幕数值在 50 Ω 负载上的实际幅度可能不同。

## DSA815 默认命令

程序使用 RIGOL DSA800 编程命令：

```text
:CALC:MARK1:STAT ON
:CALC:MARK1:MODE POS
:INIT:CONT OFF
:SENS:FREQ:CENT {hz}
:INIT:IMM
*OPC?
:CALC:MARK1:MAX:MAX
:CALC:MARK1:Y?
:CALC:MARK1:X?
```

扫描结束后发送 `:INIT:CONT ON`，让 DSA815 恢复连续扫描。

扫幅不会修改中心频率；扫频会逐点修改 DSA 中心频率，并在结束后恢复扫频前的中心频率。程序不会修改 SPAN、RBW、参考电平或 DSA 内部衰减。请确保目标 RF 峰在屏幕内且没有过载。若你已在 DSA815 的幅度修正功能中加过 48 dB，则程序里的“外置衰减”应改为 0 dB，避免重复补偿。

## 增益算法

发生器幅度记作 `Vpp`，系统阻抗为 `R = 50 Ω`：

```text
Vrms = Vpp / (2√2)
Pin(W) = Vrms² / R
Pin(dBm) = 10 log10(Pin / 1 mW)
Pout(dBm) = DSA峰值(dBm) + 48 dB
Gain(dB) = Pout(dBm) - Pin(dBm)
```

如果“每点读数”大于 1，程序先把各次 dBm 读数转成线性功率后平均，再转回 dBm；不会直接对 dBm 做错误的算术平均。

多轮平均采用同样的方法：同一幅度点的各轮 dBm 先转换为线性功率求平均，再转换回 dBm。平均增益等价于“平均输出功率 ÷ 同一输入功率”的 dB 值。轮间标准差以各轮 dB 数值计算，用于观察重复性。平均 CSV 同时保留每一轮的 DSA 峰值、校正输出、增益和输出功率，能从平均结果回查原始值。

## 数据文件

程序边测边写，不会等扫完才保存。数据默认放到：

```text
measurements\
```

每次会生成：

- `.csv`：逐点原始和换算数据，UTF-8 with BOM，可直接用 Excel 打开；扫幅文件含“轮次”列，扫频文件含“设定频率”列；
- `.json`：本次扫描参数和 SCPI 配置；
- `.png`：最终两条曲线；
- `_average.csv`：扫幅的逐幅度平均表，前半部分是平均值与轮间标准差，后半部分是各轮原始值；
- `_average.png`：只显示平均输出和平均增益的独立曲线，并用阴影显示轮间 ±1σ；
- `_average_table.png`：把全部平均数据排成一张高分辨率表格图片，适合放大查阅。

即使中途停止或后续点出错，已经写入 CSV 的点仍会保留。

也可以在命令行从任意本程序扫幅 CSV 重新生成上述三个平均文件：

```text
python hat_gain_sweep.py --average-from measurements\amplitude_sweep_3rounds_YYYYMMDD_HHMMSS.csv
```

## 自检

在程序目录打开命令行后运行：

```text
python hat_gain_sweep.py --self-test
python -m unittest -v test_core.py
```

第一条检查基础序列、功率公式和多轮平均；第二条检查多轮扫幅顺序、平均表各轮原始列、扫频点生成、DG/DSA 频率联动、功率换算和 SCPI 命令序列。
