// SPDX-License-Identifier: Apache-2.0
plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
}

dependencies {
    implementation(libs.kotlinx.serialization.json)
}

// The golden frames of the protocol contract (T1.03) and the schema are the test input of the generated types; the
// tests read them through these properties, and a changed file reruns the tests.
val protocolDir = rootDir.resolve("../protocol")

tasks.withType<Test>().configureEach {
    inputs.dir(protocolDir.resolve("fixtures")).withPropertyName("protocolFixtures")
    inputs.file(protocolDir.resolve("envelope.schema.json")).withPropertyName("protocolSchema")
    systemProperty("protocol.fixtures", protocolDir.resolve("fixtures").absolutePath)
    systemProperty("protocol.schema", protocolDir.resolve("envelope.schema.json").absolutePath)
}
