<!-- SPDX-License-Identifier: Apache-2.0 -->
# android/modules

Feature modules of the app (core and modules). `settings.gradle.kts` includes every `modules/<id>/` that holds a
`build.gradle.kts` as `:modules:<id>`; a folder without one (this README alone) is ignored. A `-PjvmOnly=true` build
includes none of them. A module declares its own manifest of permissions, hosts and surfaces (planned, T1.155).
