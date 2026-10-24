// SPDX-License-Identifier: Apache-2.0
// Gradle root of the Android app. The six pure JVM modules are always included; -PjvmOnly=true includes nothing else
// and keeps the Android Gradle plugin off the classpath (build.gradle.kts). Otherwise the eight Android modules are
// included, and every folder android/modules/<id>/ that holds a build.gradle.kts becomes :modules:<id>; an empty
// folder is allowed.
pluginManagement {
    repositories { mavenCentral() }
}

dependencyResolutionManagement {
    repositoriesMode = RepositoriesMode.FAIL_ON_PROJECT_REPOS
    // Google groups resolve only from Google Maven, everything else only from Maven Central (same regex as the
    // buildscript repositories in build.gradle.kts).
    repositories {
        exclusiveContent {
            forRepository { google() }
            filter {
                includeGroupByRegex(
                    """(com\.android|androidx|com\.google\.android|com\.google\.testing\.platform|""" +
                        """com\.google\.prefab)(\..+)?""",
                )
            }
        }
        mavenCentral()
    }
}

rootProject.name = "hud-companion"

include(":protocol", ":hud", ":input", ":module-api", ":ui-tokens", ":core")

if (providers.gradleProperty("jvmOnly").orNull != "true") {
    include(":app", ":hermeslink", ":glasses", ":confirm", ":skills", ":status", ":shell", ":audio")
    include(":third_party:g2-driver") // vendored driver, registered but not compiled yet (no plugin applied)
    file("modules").listFiles()?.filter { File(it, "build.gradle.kts").isFile }?.sortedBy { it.name }?.forEach {
        include(":modules:${it.name}")
    }
}
