# [Project scaffolding + v1] Post-surgical conversational agent

## Description
We are building a post-surgical conversational agent intended to provide support and guidance to patients during their recovery process. The agent will be capable of answering common questions over voice, based on a pre-defined knowledge base.

## Guidelines
The general guidelines and deliverables for this project can be found in the official [Source Meridian's challenge repo](https://github.com/TechSphere2026/ParticipantArtifacts/), for which I've cloned a copy into a local path already: `../ParticipantArtifacts/`. It is utterly important to follow these guidelines since evaluation rubric is based on them.
The challenge is divided into two main parts: the first part involves building a conversational agent that can answer questions based on a knowledge base, and the second part involves creating a the right layer(s) that allow users to interact with the agent.

## Usability (UX) Considerations
- Build (cold-start) latency can't exceed 15 minutes
- The expected interface is not a text chat, but a voice-based interface, so the agent should be able to handle voice input and output. Users will normally be Spanish speakers.
- Normally, end users will interact with the agent through a web interface, so the agent should be able to handle web-based interactions. However, system admins should also be able to access a web admin portal to update and manage the knowledge base and agent settings.
- It's not always the case where the user will speak clearly and under ideal conditions, so the agent should be able to handle noisy environments and accents. Robust language and speech settings should be addressed as required.
- There is no reason to assume a post-surgical patient will have a high level of technical literacy, so the agent should be easy to interact with, able to handle simple and complex questions, and provide clear answers.
- Use big and clear buttons and texts, with a clean interfacing.
- Latency is expected to be low, so the agent should be able to provide answers in a timely manner - even under multi-user concurrent setups.

## Technical Considerations
- Although this is expected to even run locally on resource-limited machines, this doesn't need to be restricted to a single tech stack. In that sense, I would go with a Dockerized setup, probably orchestrated with Docker Compose.
- The implementation already has some stack constraints in the submission guidelines (e.g. ChromaDB, BGE-M3, etc.), therefore those hard requirements must be implemented. However, the rest of the implementation can be done with any tech stack that is deemed appropriate.
- Regardless of the visual layer (UI), this needs a robust backend that supports multiple clients of different nature (e.g. web UI, API, etc.).
- The underlying API powering the agent interaction should be able to handle multiple concurrent users, and provide a low-latency response time. This is key for selecting the right tool.
- The vector (embeddings) storage for the existing knowledge base is to be stored in a local ChromaDB, while the embeddings are to be generated with BGE-M3. The knowledge base is expected to be updated over time, so the vector storage should be able to handle CRUD operations of existing vectors, including versioning, soft deletion and other relevant DB patterns.
- We must support a hybrid search pattern against the knowledge base: embedding retrieval + approx keyword search (e.g. BM25). 
- Besides the vector storage, we also need to keep track of the conversation history and other transactional and interaction data, so a separate database is required for that. The conversation history should be stored in a way that allows for easy retrieval and analysis, and should also support CRUD operations. I would think of going easy with a relational DB like SQLite, but since the implementation is expected to be service-wide scalable, we might wanto to go with PostgreSQL for future scalability.
- As a personal preference, I would like to go with Phi-3.5 (Microsoft) as the agent model. It is small and fast, although it is not as capable as other vendored models, but I want to see how far we can go with it by properly harnessing it in the agent.
- The open models are on the HF hub: [BGE-M3](https://huggingface.co/BAAI/bge-m3) and [Phi-3.5](https://huggingface.co/microsoft/phi-3.5). The models are to be downloaded and run locally, so we need to make sure the implementation is able to handle that even if they have not yet been downloaded.
- In the future, we might want to add a more capable model to the agent, so the implementation should be able to handle multiple models and vendors, and switch between them as needed.
- The model serving architecture should account for the fact that hardware can vary, so it should straight away support GPU or Metal if available, otherwise CPU. For now, we can serve locally with Ollama (which also offers Docker image).
- Docker (Compose) setup and build should include the necessary steps to download and set up the models as pre-flight or early layers, as well as any other dependencies required for the implementation. The setup should be able to handle different hardware configurations and should be able to run on both Linux and macOS.
- Every language used should have a proper dependency management system, for example, Python must use `uv`.
- There must be a general script that can be run to set up the environment depending on the OS (i.e. Docker, Compose, uv, Python, etc.).
- The Spanish TTS models are restricted to [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M). Let's handle it efficiently and hardware-aware.
- The API must define the right protocols for every type of interaction e.g. some real-time streaming might require WebSockets or WebRTC, while others might be handled with REST or gRPC. The API should be well-documented and easy to use, with clear endpoints and parameters.
- Decouple the semantic layers into multiple services if it makes sense. For instance, the model serving and the main API can go on different services, as well as the vector storage manager. For example: API (Go + Gin), model serving (e.g. Ollama loading local HF models), vector storage manager (FastAPI connected to ChromaDB), etc. The services should be able to communicate with each other efficiently and securely, and should be able to handle failures gracefully.

## Development Considerations
- Keep the codebase clean and well-organized
- Host a `docs/` folder with the organized docs
- Document the API using OpenAPI
- Variables and secrets should be managed with environment variables, and sensitive information should not be hardcoded in the codebase.
- Every service should handle quality attributes: logging, retries, etc.