// SPDX-License-Identifier: Apache-2.0
// Server link: enrolment client, device identity, the WebSocket link (planned).
plugins {
    id("com.android.library")
}

android {
    namespace = "app.hudcompanion.hermeslink"
    compileSdk = rootProject.extra["androidSdk"] as Int
    buildToolsVersion = rootProject.extra["androidBuildTools"] as String
    defaultConfig {
        minSdk = rootProject.extra["androidSdk"] as Int
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_21
        targetCompatibility = JavaVersion.VERSION_21
    }
}

dependencies {
    implementation(project(":protocol"))
    implementation(project(":core"))
    implementation(libs.okhttp)
    implementation(libs.coroutines.android)
    testImplementation(libs.okhttp.mockwebserver)
    testImplementation(libs.okhttp.tls)
    testImplementation(libs.coroutines.test)
    androidTestImplementation(libs.androidx.test.runner)
    androidTestImplementation(libs.androidx.test.junit)
    androidTestImplementation(libs.okhttp.mockwebserver)
    androidTestImplementation(libs.okhttp.tls)
}

// -PtestMinSdk reaches the debug build type only (root build.gradle.kts).
androidComponents.beforeVariants(androidComponents.selector().withBuildType("debug")) {
    it.minSdk = rootProject.extra["androidDebugMinSdk"] as Int
}
