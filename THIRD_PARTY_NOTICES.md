# 第三方依赖说明

本仓库通过 pip 安装运行依赖，不捆绑第三方库源码或二进制。第三方组件遵循各自许可证，本项目的 GPL-3.0-or-later 声明不改变其授权条款。

| 直接依赖 | 固定版本 | 用途 | 许可证与上游 |
|---|---|---|---|
| pymobiledevice3 | 11.39.0 | 设备通信、配对、隧道和 DTX | GPL-3.0-or-later；[项目](https://github.com/doronz88/pymobiledevice3)、[许可证](https://github.com/doronz88/pymobiledevice3/blob/master/LICENSE) |
| quickjs-ng（Python 绑定） | 0.18.0.1 | 执行设备返回的指标公式，Python 模块名为 quickjs | MIT；[项目](https://github.com/genotrance/quickjs-ng)、[许可证](https://github.com/genotrance/quickjs-ng/blob/master/LICENSE) |

Python 绑定使用的 [QuickJS-NG 引擎](https://github.com/quickjs-ng/quickjs) 采用 [MIT 许可证](https://github.com/quickjs-ng/quickjs/blob/master/LICENSE)。绑定和引擎是不同组件。

安装依赖时还会安装传递依赖，其许可证以对应发行包内声明为准。重新分发包含依赖的环境或二进制时，应一并保留适用的许可证与版权声明。

GPU Counter 名称和解释来自设备元数据；本文档链接的 Apple 技术资料归原权利人所有。本项目不附带设备公式归档、开发者磁盘镜像、配对资料或 Apple 系统组件。
