# Minuto Backend API

FastAPI backend for Mongolian AI Meeting Assistant.

## Features

- 🎤 **Audio Upload & Recording**: Accept audio files and WebSocket streams
- 🤖 **ASR Integration**: Whisper speech-to-text (configurable)
- 🧠 **LLM Integration**: Qwen for meeting minutes generation
- 📄 **DOCX Export**: Generate Word documents from meeting minutes
- 💾 **Audio Storage**: Persistent audio file storage

## Quick Start

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy environment file
cp ../.env.example .env

# Run development server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Visit http://localhost:8000/docs for API documentation.

## Configuration

### ASR Provider

Choose between Whisper (real) or Mock (demo):

```env
ASR_PROVIDER=whisper  # or 'mock'
WHISPER_MODEL=base    # tiny, base, small, medium, large
```

**Note**: Standard Whisper models don't support Mongolian well. For production, use a Mongolian-fine-tuned model from Hugging Face.

### LLM Provider

Choose between Qwen (real) or Mock (demo):

```env
LLM_PROVIDER=qwen   # or 'mock'
QWEN_API_KEY=your_key_here
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
QWEN_MODEL=qwen-plus
```

Get your Qwen API key from: https://dashscope.console.aliyun.com/

## API Endpoints

### Meetings

- `POST /api/meetings` - Create new meeting
- `GET /api/meetings` - List all meetings
- `GET /api/meetings/{id}` - Get meeting details
- `DELETE /api/meetings/{id}` - Delete meeting

### Audio Processing

- `POST /api/meetings/{id}/upload` - Upload audio file
- `POST /api/meetings/{id}/process` - Process audio (ASR + LLM)
- `GET /api/meetings/{id}/transcript` - Get transcript
- `GET /api/meetings/{id}/minutes` - Get meeting minutes
- `GET /api/meetings/{id}/audio` - Get audio file
- `GET /api/meetings/{id}/export.docx` - Export as DOCX

### WebSocket

- `WS /ws/meetings/{id}/audio` - Real-time audio streaming

## Testing

```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_meetings.py -v
```

## Project Structure

```
backend/
├── app/
│   ├── api/
│   │   ├── meetings.py      # Meeting endpoints
│   │   └── websocket.py     # WebSocket handler
│   ├── models/
│   │   ├── schemas.py       # Pydantic models
│   │   └── minutes.py       # Meeting minutes models
│   ├── services/
│   │   ├── asr.py           # ASR & LLM providers
│   │   ├── audio_ingest.py  # Audio processing
│   │   ├── meeting_store.py # Meeting storage
│   │   └── export_docx.py   # DOCX export
│   ├── main.py              # FastAPI app
│   └── config.py            # Configuration
├── tests/                   # Test files
└── requirements.txt         # Dependencies
```

## Deployment

### Render.com

1. Push code to GitHub
2. Create new Web Service on Render
3. Set environment variables
4. Build command: `pip install -r requirements.txt`
5. Start command: `python3 -m uvicorn app.main:app --host 0.0.0.0 --port $PORT`

### Docker

```bash
docker build -t minuto-backend .
docker run -p 8000:8000 --env-file .env minuto-backend
```

## Troubleshooting

### SSL Certificate Error (Whisper download)

If you get SSL errors when downloading Whisper models, the code automatically disables SSL verification as a workaround.

### Mongolian ASR Quality

Standard Whisper models don't support Mongolian well. Consider:
1. Using a Mongolian-fine-tuned Whisper model
2. Setting `WHISPER_MODEL` to a Hugging Face model ID
3. Using external ASR API services

## License

Private - Gerege Systems LLC
