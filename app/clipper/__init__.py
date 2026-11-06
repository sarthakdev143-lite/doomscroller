"""Long video in, finished Shorts out.

    fetch -> transcribe -> analyse signals -> pick moments -> render -> queue

Everything runs locally except the transcript (Deepgram) and the moment picking
(whichever model OpenCode is logged in to). Either can be missing: no transcript
falls back to the no-speech path, and no AI falls back to rule-based picking.
"""
