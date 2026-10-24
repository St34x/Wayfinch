// SPDX-License-Identifier: Apache-2.0
// The application module. It depends on every Android module, so its merged manifest is the app's whole manifest.
// Its unit tests check the merged release manifest, the XML rule files, the sources and the lock files
// (SAT-PL-01..11); `check` also dumps the release APK with aapt2 (SAT-PL-07).
import com.android.build.api.artifact.SingleArtifact
import java.io.ByteArrayOutputStream
import javax.inject.Inject

plugins {
    id("com.android.application")
    alias(libs.plugins.kotlin.compose)
}

android {
    // Fixed, not appId: the Kotlin packages and the relative class names in the manifests do not move with appId.
    namespace = "app.hudcompanion"
    compileSdk = rootProject.extra["androidSdk"] as Int
    buildToolsVersion = rootProject.extra["androidBuildTools"] as String
    defaultConfig {
        applicationId = providers.gradleProperty("appId").get()
        minSdk = rootProject.extra["androidSdk"] as Int
        targetSdk = rootProject.extra["androidSdk"] as Int
        versionCode = 1
        versionName = "0.1"
        manifestPlaceholders["appName"] = providers.gradleProperty("appName").get()
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_21
        targetCompatibility = JavaVersion.VERSION_21
    }
    buildFeatures { compose = true }
}

// -PtestMinSdk reaches the debug build type only (root build.gradle.kts).
androidComponents.beforeVariants(androidComponents.selector().withBuildType("debug")) {
    it.minSdk = rootProject.extra["androidDebugMinSdk"] as Int
}

dependencies {
    listOf(":shell", ":hermeslink", ":glasses", ":confirm", ":skills", ":status", ":audio", ":protocol", ":core")
        .forEach { implementation(project(it)) }
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.material3)
    implementation(libs.coroutines.android)
    testImplementation(libs.okhttp.mockwebserver)
    testImplementation(libs.okhttp.tls)
    androidTestImplementation(libs.androidx.test.runner)
    androidTestImplementation(libs.androidx.test.junit)
    androidTestImplementation(libs.okhttp.mockwebserver)
    androidTestImplementation(libs.okhttp.tls)
}

/**
 * SAT-PL-07: the release APK is not debuggable and keeps the release minSdk (the test-only override never reaches it),
 * read from the APK itself with `aapt2 dump badging`.
 */
abstract class ReleaseApkCheck : DefaultTask() {
    @get:InputFiles
    @get:PathSensitive(PathSensitivity.RELATIVE)
    abstract val apkDir: DirectoryProperty

    @get:InputFile abstract val aapt2: RegularFileProperty

    @get:Input abstract val minSdk: Property<Int>

    @get:Inject abstract val exec: ExecOperations

    @TaskAction
    fun run() {
        val apk =
            apkDir
                .get()
                .asFile
                .walk()
                .single { it.extension == "apk" }
        val out = ByteArrayOutputStream()
        exec.exec {
            commandLine(aapt2.get().asFile, "dump", "badging", apk)
            standardOutput = out
        }
        val badging = out.toString()
        if (!badging.startsWith("package: name=")) throw GradleException("aapt2 printed no badging for $apk")
        if ("application-debuggable" in badging) throw GradleException("SAT-PL-07: the release APK is debuggable")
        if ("minSdkVersion:'${minSdk.get()}'" !in badging.lines()) {
            throw GradleException("SAT-PL-07: the release APK's minSdk is not ${minSdk.get()}")
        }
    }
}

androidComponents {
    onVariants(selector().withBuildType("release")) { variant ->
        val manifest = variant.artifacts.get(SingleArtifact.MERGED_MANIFEST)
        tasks.withType<Test>().configureEach {
            inputs.file(manifest).withPropertyName("satMergedReleaseManifest")
            jvmArgumentProviders.add(
                CommandLineArgumentProvider { listOf("-Dsat.mergedManifest=${manifest.get().asFile.absolutePath}") },
            )
        }
        val apkCheck =
            tasks.register<ReleaseApkCheck>("checkReleaseApk") {
                apkDir.set(variant.artifacts.get(SingleArtifact.APK))
                aapt2.set(sdkComponents.sdkDirectory.map { it.file("build-tools/${android.buildToolsVersion}/aapt2") })
                minSdk.set(rootProject.extra["androidSdk"] as Int)
            }
        tasks.named("check") { dependsOn(apkCheck) }
    }
}

tasks.withType<Test>().configureEach {
    val androidRoot = rootDir
    jvmArgumentProviders.add(CommandLineArgumentProvider { listOf("-Dsat.androidRoot=${androidRoot.absolutePath}") })
    inputs
        .files(
            fileTree(androidRoot) {
                include("**/src/**", "**/gradle.lockfile")
                exclude("build/**", "*/build/**", "modules/*/build/**")
            },
        ).withPropertyName("satSources")
}
