# AI Robot Backend

A modular backend for building an autonomous, expressive AI robot inspired by robots such as Cozmo.

The project extends the Xiaozhi ecosystem beyond a voice assistant into a persistent embodied agent capable of:

* voice conversation
* autonomous behaviors
* personality and emotional state
* long-term memory
* vision and person recognition
* semantic robot actions
* animations and expressions
* sensor awareness
* MCP tool discovery
* local or cloud LLM integration
* safe robot motion
* simulated robot development without physical hardware

The goal is to build a robot that does more than wait for commands.

It should be able to perceive its environment, remember people, decide what to do, express personality, initiate interactions, and continue functioning even when an LLM is unavailable.

## Architecture

```text
                         ┌────────────────────┐
                         │       Memory       │
                         └─────────┬──────────┘
                                   │
 Camera ──► Vision ──► World Model ◄──── Conversation
                         │                 │
                         │                 ▼
                   ┌─────▼─────┐         LLM
                   │ Behavior  │          │
                   │  Engine   │◄─────────┘
                   └─────┬─────┘
                         │
                         ▼
                  Action Executor
                         │
                  ┌──────▼──────┐
                  │ Safety Layer │
                  └──────┬──────┘
                         │
                     MCP Tools
                         │
              ───────────┼───────────
                    Network Boundary
              ───────────┼───────────
                         │
                  Robot Firmware
                  │      │      │
               Motion Sensors Display
                  │
              Local Safety
                  │
                Motors
```

The LLM is deliberately not the robot's operating system.

Low-level control, safety, autonomous behavior scheduling, tracking, and timing-critical operations remain deterministic.

The LLM handles higher-level tasks such as conversation, interpretation, planning, and semantic tool selection.

## Core Principles

### Semantic actions, not raw motors

The backend never asks an LLM to directly control PWM, wheel voltage, or servo timing.

Instead it exposes high-level actions such as:

```python
await robot.move(distance_m=0.30)
await robot.turn(angle_deg=45)
await robot.look_at(target_id="person-1")
await robot.follow_person(person_id="person-1")
await robot.play_animation("excited")
await robot.stop()
```

The robot firmware translates those commands into actual motor and servo control.

### Safety is deterministic

Safety rules cannot be overridden by the LLM.

Examples include:

* cliff detection
* obstacle protection
* movement limits
* watchdog timeout
* stale sensor rejection
* emergency stop
* connection-loss stop
* action timeout

Critical protection must also exist locally in the robot firmware.

Network-side safety alone is not sufficient.

### Autonomous without an LLM

The robot should remain functional when the LLM is unavailable.

It can still:

* look around
* react to people
* react to touch
* monitor battery
* stop at obstacles
* avoid cliffs
* play animations
* enter sleep states
* return to a charger
* execute direct operator commands

The behavior engine provides this autonomy.

## Major Components

### Robot Registry

Tracks connected robots and their capabilities.

Each robot has:

* identity
* firmware version
* protocol version
* capabilities
* connection state
* telemetry
* sensors
* battery
* pose
* activity
* current actions

Capabilities are dynamically discovered through MCP.

### MCP Robot Tools

A robot can expose semantic capabilities such as:

```text
robot.get_status

robot.motion.move
robot.motion.turn
robot.motion.stop

robot.head.set_angle
robot.head.look_at

robot.lift.set_position

robot.expression.set
robot.animation.play

robot.camera.capture

robot.sensor.get_distance
robot.sensor.get_imu
robot.sensor.get_cliff

robot.power.get_battery
```

The backend discovers available tools rather than assuming every robot has identical hardware.

### Action System

All robot commands pass through the action layer.

An action has a lifecycle:

```text
PENDING
    ↓
STARTING
    ↓
RUNNING
    ↓
 ┌──┴──────────────┐
 ▼                 ▼
SUCCEEDED         FAILED

CANCELLED
TIMED_OUT
REJECTED
```

Actions also claim hardware resources such as:

```text
DRIVE
HEAD
LIFT
DISPLAY
AUDIO
CAMERA
```

This prevents conflicting operations from attempting to control the same subsystem simultaneously.

### Behavior Engine

The robot does not need an LLM to decide every action.

A utility-based behavior engine continuously evaluates possible behaviors.

Example:

```text
Behavior                 Score

GoToCharger              0.94
GreetPerson              0.71
Explore                  0.36
LookAround               0.28
Bored                    0.15
```

The highest valid behavior wins.

Initial behaviors include:

* idle
* look around
* explore
* greet person
* look at person
* approach person
* follow person
* react to touch
* react to sound
* investigate object
* boredom behavior
* low battery
* go to charger
* charging
* wake
* sleep

Behaviors support:

* utility scoring
* priorities
* cooldowns
* interruption
* resource ownership
* preemption
* minimum execution time
* maximum execution time
* deterministic testing

### Personality

Each robot can have relatively stable personality traits.

Example:

```yaml
personality:
  sociability: 0.8
  curiosity: 0.9
  playfulness: 0.7
  boldness: 0.6
  patience: 0.5
  energy_baseline: 0.8
```

Personality influences behavior selection.

A highly curious robot may explore more frequently.

A highly social robot may prefer interacting with people.

Personality never overrides safety.

### Internal Emotional State

The backend maintains lightweight internal behavior variables such as:

```text
valence
arousal
curiosity
boredom
social_need
confidence
energy
```

These are not intended to model human emotions.

They are control signals that influence behavior and expression.

For example:

```text
No interaction
    ↓
boredom increases
    ↓
LookAround / Explore becomes more likely
```

### Expressions and Animations

The robot can use semantic expressions:

```text
neutral
happy
excited
curious
confused
sad
sleepy
surprised
annoyed
scared
focused
```

Animations combine multiple robot subsystems.

Example:

```yaml
excited_greeting:
  expression: excited

  sequence:
    - head: 10
      duration_ms: 150

    - head: -5
      duration_ms: 150

    - body_wiggle: true
      duration_ms: 300

    - sound: chirp
```

Animations are data-driven so new animations can be created without changing Python code.

### Vision

The vision subsystem can process robot camera frames and update the world model.

The architecture supports:

* OpenCV
* object detection
* face detection
* face recognition
* person tracking
* target tracking
* visual inspection
* future SLAM integration

Vision events include:

```text
PersonDetected
PersonLost

FaceDetected
KnownPersonRecognized
UnknownPersonDetected

ObjectDetected
ObjectLost
```

Vision coordinates use normalized values:

```text
x = 0.0 ... 1.0
y = 0.0 ... 1.0
```

This allows different camera resolutions to use the same tracking logic.

### World Model

The backend maintains a representation of what the robot currently knows about its environment.

Example:

```text
World
│
├── people
│   ├── person-1
│   └── person-2
│
├── objects
│   ├── cube-1
│   └── chair-1
│
├── obstacles
│
├── locations
│
├── current interaction
│
└── attention target
```

World entities can contain:

```text
id
type
attributes
confidence
position
first_seen
last_seen
```

### Memory

The memory system contains several different layers.

#### Working Memory

Short-lived context for the current interaction.

#### Episodic Memory

Events experienced by the robot.

Examples:

```text
Met Ahmad
Played with cube
Saw unknown person
Failed to reach charger
User praised robot
```

#### Semantic Memory

Stable learned information.

Examples:

```text
Ahmad prefers concise responses.
The charging dock is near the desk.
The red cube belongs to Ahmad.
```

#### Person Memory

Information associated with known people.

```text
person_id
name
first_seen
last_seen
interaction_count
familiarity
facts
```

SQLite is used initially, with storage abstractions designed to allow PostgreSQL later.

Vector search is optional.

The robot must continue functioning without embeddings.

### LLM Integration

The backend can use Xiaozhi-compatible LLM providers.

Possible backends include:

* OpenAI-compatible APIs
* Ollama
* LM Studio
* vLLM
* cloud providers
* local models

The LLM receives a bounded context containing:

```text
robot state
world state
current interaction
relevant memories
available tools
safety restrictions
```

Raw telemetry history is not sent to the model.

### Voice Pipeline

The voice pipeline reuses the Xiaozhi architecture.

```text
Microphone
   │
   ▼
  VAD
   │
   ▼
  ASR
   │
   ▼
Robot Agent
   │
   ├── LLM
   ├── Memory
   └── Tools
   │
   ▼
  TTS
   │
   ▼
Speaker
```

Conversation states include:

```text
IDLE
LISTENING
THINKING
SPEAKING
INTERRUPTED
```

Barge-in is supported.

If the user begins speaking while the robot is talking:

```text
TTS stops
    ↓
robot starts listening
    ↓
new utterance is processed
```

### Robot Simulator

Physical hardware is not required for backend development.

The project includes a robot simulator capable of exposing the same semantic MCP tools as a real robot.

The simulator can model:

* movement
* pose
* head movement
* lift position
* battery
* charging
* distance sensors
* cliff sensors
* IMU
* camera
* expressions
* animations

Example:

```bash
python -m robot.simulator \
  --server ws://localhost:8000/xiaozhi/v1/ \
  --robot-id robot-dev-01
```

Scenarios can simulate events such as:

```text
person_enters_room
person_leaves_room
battery_low
obstacle_during_move
cliff_during_move
charger_found
```

Example:

```bash
python -m robot.simulator \
  --server ws://localhost:8000/xiaozhi/v1/ \
  --robot-id robot-dev-01 \
  --scenario person_enters_room
```

## Example Interaction

Human:

```text
Come a little closer.
```

Pipeline:

```text
Microphone
   ↓
ASR
   ↓
"Come a little closer."
   ↓
LLM
   ↓
robot.move(distance_m=0.25)
   ↓
RobotActionExecutor
   ↓
SafetyPolicy
   ↓
MCP
   ↓
Robot firmware
   ↓
Motion controller
   ↓
Motors
```

The LLM never sees motor PWM values.

## Autonomous Interaction Example

A person enters the room.

```text
Camera
   ↓
Vision
   ↓
PersonDetected
   ↓
WorldState updated
   ↓
Behavior Engine
```

Candidate scores:

```text
GreetPerson     0.88
LookAtPerson    0.76
Explore         0.23
LookAround      0.12
```

`GreetPerson` wins.

The robot may then:

```text
turn toward person
     ↓
look at face
     ↓
play excited animation
     ↓
say hello
```

No explicit user command is required.

## Autonomy Modes

The backend supports different autonomy levels.

### OFF

Only explicit commands are executed.

### PASSIVE

The robot can react and express itself but does not move autonomously.

### NORMAL

Normal social and environmental autonomous behavior.

### FULL

Allows exploration, following, and more active autonomous behavior.

## Safety Model

Robot safety exists at two levels.

### Backend

The backend checks:

* movement limits
* sensor freshness
* obstacle state
* cliff state
* action conflicts
* command timeout
* connection health
* autonomy permissions

### Firmware

The physical robot must independently enforce:

* cliff detection
* motor watchdog
* emergency stop
* acceleration limits
* movement bounds
* current protection where available
* thermal protection where available
* communication timeout

The backend is never treated as the only safety layer.

## Development Stack

Primary backend technologies:

```text
Python 3.12+
asyncio
Pydantic
pytest
pytest-asyncio
SQLite
OpenCV
MCP / JSON-RPC
WebSockets
```

Development tooling:

```text
Ruff
mypy / pyright
Docker
Docker Compose
```

## Running

Clone the repository:

```bash
git clone <repository-url>
cd <repository-name>
```

Create a virtual environment:

```bash
python3 -m venv .venv

source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Start the backend:

```bash
python app.py
```

Then start a simulated robot:

```bash
python -m robot.simulator \
  --server ws://localhost:8000/xiaozhi/v1/ \
  --robot-id robot-dev-01
```

Exact commands may change while the project is under active development.

See:

```text
docs/robot-getting-started.md
```

for the current setup instructions.

## Testing

Run the test suite:

```bash
pytest
```

Lint:

```bash
ruff check .
```

Type checking:

```bash
mypy .
```

The project contains:

* unit tests
* protocol tests
* behavior tests
* action/safety tests
* simulator tests
* integration tests
* end-to-end robot scenarios

Cloud APIs should not be required for CI.

Fake ASR, LLM, TTS, vision, and robot implementations are used for deterministic testing.

## Repository Structure

The exact layout may evolve, but the robot-specific subsystem is organized approximately as:

```text
robot/

├── actions/
│   ├── executor.py
│   ├── models.py
│   └── queue.py
│
├── behavior/
│   ├── engine.py
│   ├── scheduler.py
│   └── behaviors/
│
├── devices/
│   ├── registry.py
│   └── capabilities.py
│
├── events/
│   ├── bus.py
│   └── models.py
│
├── memory/
│
├── personality/
│
├── protocol/
│
├── safety/
│
├── simulator/
│
├── state/
│
└── vision/
```

## Roadmap

### Foundation

* Robot registry
* MCP capability discovery
* Robot state store
* Internal event bus
* Robot simulator

### Robot control

* Action executor
* Resource locking
* Safety policy
* Emergency stop
* Motion lifecycle

### Autonomy

* World model
* Behavior engine
* Utility scoring
* Autonomous behaviors
* Autonomy modes

### Personality

* Personality traits
* Emotional state
* Expressions
* Data-driven animations

### Vision

* Camera processing
* Person detection
* Object detection
* Tracking
* Face recognition
* Look-at behavior
* Follow behavior

### Memory

* Working memory
* Episodic memory
* Semantic memory
* Person memory
* Optional embedding search

### AI

* Robot Agent
* LLM tools
* Memory context
* Local LLM support
* Proactive conversation

### Voice

* ASR
* TTS
* VAD
* Interruption / barge-in
* Speech animation

### Development

* Management REST API
* Live event stream
* Development dashboard
* Docker deployment
* Metrics
* E2E simulator CI

### Hardware

* Custom Xiaozhi robot firmware
* Drive controller
* Head controller
* Display/eyes
* Camera
* IMU
* Distance sensors
* Cliff sensors
* Battery monitoring
* Charging dock

## Project Status

This project is under active development.

The architecture intentionally supports development against a simulator before committing to final robot hardware.

Features should only be considered complete when covered by passing tests or validated on physical hardware.

See:

```text
PROJECT_STATUS.md
```

for the current implementation status.

## Upstream

This project builds on ideas and infrastructure from the Xiaozhi ecosystem:

* `xinnan-tech/xiaozhi-esp32-server`
* `78/xiaozhi-esp32`

Robot-specific architecture is kept as isolated as practical to make future upstream updates easier to integrate.

## Why This Project Exists

Most AI assistants follow this pattern:

```text
Human speaks
    ↓
AI responds
    ↓
wait
```

An embodied robot needs something different:

```text
        ┌───────────────┐
        │   Perception  │
        └───────┬───────┘
                ▼
           World Model
                │
       ┌────────┴────────┐
       ▼                 ▼
   Behavior             LLM
    Engine               │
       │                 │
       └────────┬────────┘
                ▼
             Actions
                │
                ▼
              Robot
                │
                └──────────► Perception
```

The objective is not simply to build a voice assistant with wheels.

The objective is to build a robot that has persistent state, perception, memory, autonomous behavior, expressive movement, personality, and the ability to interact naturally with its environment.
