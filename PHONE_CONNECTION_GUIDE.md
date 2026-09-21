# Phone Connection Guide

## Overview

This guide explains how to connect your phone to the ClinicalScribe desktop application to record audio on your phone and have it transcribed on your desktop computer.

## Requirements

- Desktop computer and phone must be on the **same Wi-Fi network**
- Desktop: Windows with Python 3.9+
- Phone: Modern browser (Chrome, Safari, Firefox, Edge)
- Microphone permission on phone
- Windows Firewall configured to allow incoming connections

## Desktop Setup

### 1. Install Dependencies

```bash
cd C:\ClinicalScribe
python -m pip install -r requirements.txt
```

### 2. Configure Environment

Create a `.env` file in the project root with your transcription API key:

```
OPENAI_API_KEY=your-key-here
```

See `.env.example` for all available options.

### 3. Start the Server

```bash
python main.py
```

The application will:
- Start a local server on port 8000
- Display your LAN IP address
- Show a QR code for phone access
- Display a 6-digit pairing code

**Example output:**
```
Server running at:
  Desktop: http://localhost:8000
  Phone:   http://192.168.1.100:8000/phone

Scan this QR code with your phone:
[QR CODE DISPLAYED HERE]

Pairing code: 123456
(Code expires in 15 minutes)
```

### 4. Configure Windows Firewall

**First time only:** Allow Python through Windows Firewall when prompted, or manually:

1. Open Windows Defender Firewall → Advanced Settings
2. Click "Inbound Rules" → "New Rule"
3. Select "Program" → Browse to your `python.exe`
4. Allow the connection
5. Apply to "Private" networks only (not Public)
6. Name it "ClinicalScribe Python"

**Important:** Only allow connections on Private networks, not Public networks.

## Phone Setup

### 1. Connect to Same Wi-Fi

Ensure your phone is on the same Wi-Fi network as your desktop computer. This will NOT work over cellular data or different networks.

### 2. Scan QR Code or Enter URL

**Option A - QR Code (Recommended):**
1. Open your phone's camera app
2. Point at the QR code on your desktop screen
3. Tap the notification/link that appears

**Option B - Manual URL:**
1. Open your phone's browser
2. Type the URL shown under "Phone:" on your desktop
   - Example: `http://192.168.1.100:8000/phone`

### 3. Enter Pairing Code

When the page loads, you'll see a pairing code entry screen:
1. Enter the 6-digit code shown on your desktop
2. Tap "Pair Device"
3. The code expires after 15 minutes for security

### 4. Grant Microphone Permission

Your browser will request microphone access:
- **iPhone Safari:** Tap "Allow"
- **Android Chrome:** Tap "Allow"

**Note:** If you previously denied permission, you must enable it in your phone's settings:
- **iPhone:** Settings → Safari → Camera & Microphone → Allow
- **Android:** Settings → Apps → Chrome → Permissions → Microphone → Allow

## Recording Audio

### 1. Start Recording

1. Tap the red **"Start Recording"** button
2. The button changes to **"Stop Recording"** (pulsing red)
3. A timer shows the recording duration
4. Audio is recorded locally on your phone

### 2. Stop Recording

1. Tap **"Stop Recording"**
2. The audio file is prepared for upload
3. You'll see a preview with duration and size

### 3. Upload to Desktop

1. Review the recording details
2. Tap **"Upload for Transcription"**
3. Progress bar shows upload status
4. Large files are uploaded in chunks

### 4. Transcription

Once uploaded:
- Desktop saves the audio to `data/audio/`
- Transcription starts automatically
- Progress appears on phone
- Completed transcript shows on desktop

## File Storage

### Audio Files
- Location: `C:\ClinicalScribe\data\audio\`
- Format: Original format from phone (WebM, M4A, etc.)
- Naming: `recording_YYYYMMDD_HHMMSS.ext`

### Transcripts
- Location: `C:\ClinicalScribe\data\artifacts\`
- Format: JSON with segments, timestamps, speakers
- Naming: Same as audio file with `.json` extension

## Troubleshooting

### "Cannot connect to server"

**Possible causes:**
1. **Different networks:** Phone and desktop must be on same Wi-Fi
2. **Windows Firewall:** Allow Python through firewall (see step 4 above)
3. **Wrong IP address:** The server displays the correct LAN IP - verify it matches
4. **Port conflict:** Another app is using port 8000
   - Stop other apps or change port in `.env`: `SERVER_PORT=8001`

**To test connectivity:**
```bash
# On desktop, find your IP:
ipconfig

# Look for "IPv4 Address" under your Wi-Fi adapter
# Should match the IP shown by ClinicalScribe
```

### "Pairing code invalid or expired"

**Solutions:**
1. **Expired code:** Codes expire after 15 minutes
   - Disable and re-enable phone mode on desktop to get a new code
2. **Wrong code:** Double-check the 6 digits on your desktop
3. **Desktop restarted:** Restart generates a new code

### "Microphone permission denied"

**iPhone:**
1. Settings → Safari → Camera & Microphone
2. Set to "Ask" or "Allow"
3. Reload the page in Safari

**Android:**
1. Settings → Apps → Chrome (or your browser)
2. Permissions → Microphone → Allow
3. Reload the page

### "Recording failed" or "Upload failed"

**Possible causes:**
1. **Browser compatibility:** Use Chrome, Safari, Firefox, or Edge
2. **Insecure context:** The connection uses HTTP on local network (this is normal)
3. **Network interruption:** Reconnect and try again
4. **Disk space:** Check desktop has free space in `data/audio/`

**Recovery:**
- Audio is stored on phone until successfully uploaded
- You can retry upload without re-recording
- Check desktop console for detailed error messages

### "Transcription failed"

**Possible causes:**
1. **No API key:** Check `.env` file has `OPENAI_API_KEY`
2. **Invalid API key:** Verify key is correct and active
3. **Audio format unsupported:** Project supports WebM, M4A, WAV, MP3
4. **API quota exceeded:** Check your OpenAI account

**Recovery:**
- Audio file is already saved in `data/audio/`
- Transcription can be retried from desktop without re-uploading
- Check desktop logs for specific error

### Determining Your LAN Address

If you need to manually find your desktop's LAN IP address:

**Windows:**
```bash
ipconfig
```
Look for "IPv4 Address" under your active Wi-Fi adapter (usually starts with 192.168.x.x or 10.x.x.x)

**The application automatically detects and displays this for you.**

## Security Notes

- Server binds to `0.0.0.0` (all interfaces) only when phone mode is enabled
- Pairing codes expire after 15 minutes
- Only devices with valid pairing code can upload
- Uploads are validated for size, format, and checksums
- Audio files are saved with sanitized filenames to prevent path traversal
- **Recommendation:** Only enable phone mode when actively recording
- Audio is sent to OpenAI for transcription (if using OpenAI API)

## Stopping the Server

Press `Ctrl+C` in the terminal where `main.py` is running.

## Advanced Configuration

Edit `.env` to customize:

```env
# Server
SERVER_HOST=0.0.0.0
SERVER_PORT=8000

# Upload limits
MAX_UPLOAD_SIZE_MB=500
CHUNK_SIZE_MB=5

# Pairing
PAIRING_CODE_EXPIRY_MINUTES=15

# Transcription
OPENAI_API_KEY=your-key-here
WHISPER_MODEL=whisper-1
```

## Testing Without Phone

You can test the API endpoints using curl:

```bash
# Enable phone mode
curl -X POST http://localhost:8000/api/v1/phone/enable

# Check status
curl http://localhost:8000/api/v1/phone/status
```

## Support

For issues or questions:
1. Check desktop console logs for detailed errors
2. Verify all requirements are met
3. Try with a different phone/browser
4. Check GitHub issues at [repository URL]
