# Generated by Django 3.1.7 on 2021-12-20 09:47

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="TOCHomepage",
            fields=[
                ("id", models.PositiveIntegerField(primary_key=True, serialize=False)),
                ("icon_url", models.URLField()),
                ("category", models.CharField(max_length=255)),
                ("description", models.CharField(max_length=255)),
                ("data1_title", models.CharField(max_length=255)),
                ("data1_url", models.URLField()),
                ("data2_title", models.CharField(max_length=255)),
                ("data2_url", models.URLField()),
                ("data3_title", models.CharField(max_length=255)),
                ("data3_url", models.URLField()),
            ],
        ),
    ]
