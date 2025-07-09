# Portacode

**An AI-first, mobile-first IDE and admin tool, made with love and passion by software engineers, for software engineers.**

Portacode transforms any computer into a remotely accessible development environment. Access your home lab from your phone, code on your desktop from anywhere, or help a colleague debug their server - all through a beautiful web interface designed for the modern developer.

## ✨ Why Portacode?

- **🤖 AI-First**: Built from the ground up with AI integration in mind
- **📱 Mobile-First**: Code and administrate from your phone or tablet
- **🌍 Global Access**: Connect to your devices from anywhere with internet
- **🔐 Secure**: HTTPS encrypted with RSA key authentication
- **⚡ Fast Setup**: Get connected in under 60 seconds
- **🔄 Always Connected**: Automatic reconnection and persistent service options
- **🆓 Free Account**: Create your account and start connecting immediately
- **🖥️ Cross-Platform**: Works on Windows, macOS, and Linux

## 🚀 Quick Start

### 1. Install Portacode

```bash
pip install portacode
```

### 2. Connect Your Device

```bash
portacode connect
```

Follow the on-screen instructions to:
- Visit [https://remote.portacode.com](https://remote.portacode.com)
- Create your free account
- Add your device using the generated key
- Start coding and administrating!

### 3. Access Your Development Environment

Once connected, you can:
- Open terminal sessions from the web dashboard
- Execute commands remotely
- Monitor system status
- Access your development environment from any device

## 💡 Use Cases

### Remote Development
```bash
# Connect your development machine
portacode connect

# Now code, build, and debug from anywhere - even your phone
```

### Server Administration
```bash
# Install as a service for persistent connection
portacode service install

# Your server is now accessible 24/7 from the web dashboard
```

### Mobile Development
```bash
# Connect your desktop/laptop
portacode connect

# Code on-the-go from your mobile device with a full IDE experience
```

### Team Collaboration
```bash
# Connect shared development environments
portacode connect

# Enable team members to access shared resources securely
```

## 🔧 Essential Commands

### Basic Usage
```bash
# Start a connection (runs until closed)
portacode connect

# Run connection in background
portacode connect --detach

# Get help
portacode --help
```

### Service Management
```bash
# Install persistent service (auto-start on boot)
portacode service install

# Check service status
portacode service status

# Stop the service
portacode service stop

# Remove the service
portacode service uninstall
```

## 🌐 Web Dashboard

Access your connected devices at [https://remote.portacode.com](https://remote.portacode.com)

**Current Features:**
- Real-time terminal access
- System monitoring
- Device management
- Multi-device switching
- Secure authentication

**Coming Soon:**
- AI-powered code assistance
- Mobile-optimized IDE interface
- File management and editing
- Collaborative development tools

## 🔐 Security

- **RSA Key Authentication**: Each device gets a unique RSA key pair
- **HTTPS Encrypted**: All communication is encrypted in transit
- **No Passwords**: Key-based authentication eliminates password risks
- **Revocable Access**: Remove devices instantly from the web dashboard
- **Local Key Storage**: Private keys never leave your device

## 🆘 Troubleshooting

### Connection Issues
```bash
# Check if another connection is running
portacode connect

# View service logs
portacode service status --verbose
```

### Clipboard Issues (Linux)
```bash
# Install clipboard support
sudo apt-get install xclip
```

### Key Management
Key files are stored in:
- **Linux**: `~/.local/share/portacode/keys/`
- **macOS**: `~/Library/Application Support/portacode/keys/`
- **Windows**: `%APPDATA%\portacode\keys\`

## 🌱 Early Stage Project

**Portacode is a young project with big dreams.** We're building the future of remote development and mobile-first coding experiences. As a new project, we're actively seeking:

- **👥 Community Feedback**: Does this solve a real problem for you?
- **🤝 Contributors**: Help us build the IDE of the future
- **📢 Early Adopters**: Try it out and let us know what you think
- **💡 Feature Ideas**: What would make your remote development workflow better?

**Your support matters!** Whether you contribute code, report bugs, share ideas, or simply let us know that you find value in what we're building - every bit of feedback helps us decide whether to continue investing in this vision or focus on other projects.

## 📞 Get In Touch

- **Email**: hi@menas.pro
- **Support**: support@portacode.com
- **GitHub**: [https://github.com/portacode/portacode](https://github.com/portacode/portacode)

## 🤝 Contributing

We welcome all forms of contribution:
- 🐛 **Bug Reports**: Found something broken? Let us know!
- ✨ **Feature Requests**: What would make Portacode better for you?
- 📖 **Documentation**: Help others get started
- 💻 **Code Contributions**: Help us build the future of remote development
- 💬 **Feedback**: Tell us if you find this useful!

Check out our [GitHub repository](https://github.com/portacode/portacode) to get started.

## 📄 License


---

**Get started today**: `pip install portacode && portacode connect`

*Built with ❤️ and ☕ by passionate software engineers* 