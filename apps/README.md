# Application-owned workspace

Each target application uses `apps/<app>/`. The four-stage controller and its
Coding Agents create and validate the application-owned runtime declaration,
Collector, engine adapter, command candidates, evidence, commands, guide, and
Skill here. Shared ANIS code must not contain application-name conditionals.

Do not copy `templates/app/` blindly. It documents the file formats; Stage 1
must inspect the real installed application and write truthful values.
