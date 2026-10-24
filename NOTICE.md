# NOTICE

**Distributed-TTS-Bot**  
Copyright 2026 Maximilian2022 and Distributed-TTS-Bot Contributors

This product is licensed under the **Apache License, Version 2.0**.  
See the [LICENSE](LICENSE) file for the full license text.

---

## Third-Party Software and Voice Model Attributions / サードパーティ帰属表示

### 1. Open JTalk HTS Voice Models (`openjtalk_voices/`)
- **Included Models**: Mei (angry, bashful, happy, normal, sad), Takumi (angry, happy, normal, sad), nitech
- **Copyright**: (c) 2009-2018 Nagoya Institute of Technology Department of Computer Science
- **License**: [Modified BSD License (3-Clause BSD)](https://opensource.org/licenses/BSD-3-Clause)
- Redistribution and use in source and binary forms, with or without modification, are permitted provided that the copyright notice and this list of conditions are retained.

### 2. edge-tts
- **Copyright**: (c) 2021-2024 rany
- **License**: [GNU Lesser General Public License v3 (LGPLv3)](https://www.gnu.org/licenses/lgpl-3.0.html)
- Used as an external library via dynamic import without modifying `edge-tts` source code.

### 3. alkana (Optional Dependency / オプショナル依存)
- **Copyright**: (c) cod-sushi
- English-to-Katakana dictionary based on `bep-eng.dic` (c) 1999-2002 Bilingual Emacspeak Project
- **License**: [GNU General Public License v2 (GPLv2)](https://www.gnu.org/licenses/old-licenses/gpl-2.0.html)
- **Note**: This dependency is completely optional. Distributed-TTS-Bot core operates independently without `alkana`.

### 4. Other Python Libraries
- **discord.py**: MIT License (Copyright (c) 2015-present Rapptz)
- **aiohttp**: Apache License 2.0
- **aiosqlite**: MIT License
- **PyYAML**: MIT License
- **emoji**: BSD 3-Clause License
- **janome**: Apache License 2.0
- **SudachiPy & sudachidict_core**: Apache License 2.0
- **zstandard**: BSD 3-Clause License
- **google-cloud-texttospeech**: Apache License 2.0
- **azure-cognitiveservices-speech**: MIT License

---

## Voice Synthesis Engines & Character Guidelines Disclaimer / 音声合成エンジン・キャラクター利用規約免責

### [English]
Distributed-TTS-Bot provides integration interfaces for third-party text-to-speech engines including **VOICEVOX**, **COEIROINK**, and **SHAREVOX**.

Users and server administrators of this software are **solely responsible** for complying with the respective Terms of Service, Character Usage Guidelines, and attribution requirements for all synthesized audio output:

- **VOICEVOX**: [https://voicevox.hiroshiba.jp/](https://voicevox.hiroshiba.jp/)  
  Each character (e.g., Zundamon, Shikoku Metan) has specific usage guidelines, credit requirements, and commercial/non-commercial restrictions.
- **COEIROINK**: [https://coeiroink.com/](https://coeiroink.com/)  
  Each voice model (e.g., Tsukuyomi-chan, MANA, MYCOEIROINK characters) has individual character usage terms established by their respective creators.
- **SHAREVOX**: [https://www.sharevox.net/](https://www.sharevox.net/)  
  Usage must adhere to the SHAREVOX terms and character guidelines.

The authors and contributors of Distributed-TTS-Bot assume no liability for any misuse, copyright infringement, or violations of character terms resulting from the use of synthesized voices.

### [日本語]
Distributed-TTS-Bot は、**VOICEVOX**、**COEIROINK**、**SHAREVOX** などの外部音声合成エンジンとの連携制御インターフェースを提供します。

本ソフトウェアによって生成された音声の利用、配信、公開にあたっては、各音声合成エンジンおよび各キャラクターの利用規約（クレジット表記義務、商用利用条件、禁止事項等）を**利用者自身の責任において遵守**する必要があります：

- **VOICEVOX**: [https://voicevox.hiroshiba.jp/](https://voicevox.hiroshiba.jp/)  
  ずんだもん、四国めたん等の各キャラクターには、個別のキャラクター利用規約およびクレジット表記ルールが定められています。
- **COEIROINK**: [https://coeiroink.com/](https://coeiroink.com/)  
  つくよみちゃん、MANA、および MYCOEIROINK の各話者モデルには、各制作者様が定めた固有のキャラクター利用規約が存在します。
- **SHAREVOX**: [https://www.sharevox.net/](https://www.sharevox.net/)  
  各キャラクターの利用ガイドラインを遵守してください。

本ソフトウェアの作者およびコントリビューターは、生成された音声の利用に伴ういかなるトラブル、規約違反、著作権侵害等について一切の責任を負いません。
